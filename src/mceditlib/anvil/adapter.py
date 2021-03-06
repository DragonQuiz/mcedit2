"""
    adapter
"""
from __future__ import absolute_import, division, print_function
import logging
import os
import random
import struct
import traceback
import weakref

import numpy
import zlib
import time

from mceditlib import nbt
from mceditlib.anvil.entities import PCEntityRef, PCTileEntityRef
from mceditlib.anvil.worldfolder import AnvilWorldFolder
from mceditlib.blocktypes import pc_blocktypes
from mceditlib.geometry import Vector, BoundingBox
from mceditlib import nbtattr
from mceditlib.exceptions import PlayerNotFound
from mceditlib.revisionhistory import RevisionHistory


log = logging.getLogger(__name__)

# --- Constants ---

GAMETYPE_SURVIVAL = 0
GAMETYPE_CREATIVE = 1

VERSION_MCR = 19132
VERSION_ANVIL = 19133

# --- Exceptions ---


class AnvilChunkFormatError(IOError):
    """
    Raised when an Anvil chunk's data is not as expected
    """


class SessionLockLost(IOError):
    """
    Raised when the session lock is lost because another program (Minecraft) opened the level
    while we were editing it.
    """

# --- Helper functions ---


def unpackNibbleArray(dataArray):
    s = dataArray.shape
    unpackedData = numpy.empty((s[0], s[1], s[2] * 2), dtype='uint8')

    unpackedData[:, :, ::2] = dataArray
    unpackedData[:, :, ::2] &= 0xf
    unpackedData[:, :, 1::2] = dataArray
    unpackedData[:, :, 1::2] >>= 4
    return unpackedData


def packNibbleArray(unpackedData):
    packedData = numpy.array(unpackedData.reshape(16, 16, unpackedData.shape[2] / 2, 2))
    packedData[..., 1] <<= 4
    packedData[..., 1] |= packedData[..., 0]
    return numpy.array(packedData[:, :, :, 1])


def deflate(data):
    # zobj = zlib.compressobj(6,zlib.DEFLATED,-zlib.MAX_WBITS,zlib.DEF_MEM_LEVEL,0)
    # zdata = zobj.compress(data)
    # zdata += zobj.flush()
    # return zdata
    return zlib.compress(data)


def inflate(data):
    return zlib.decompress(data)


def sanitizeBlocks(section, blocktypes):
    return
    # # change grass to dirt where needed so Minecraft doesn't flip out and die
    # grass = section.Blocks == blocktypes.Grass.ID
    # grass |= section.Blocks == blocktypes.Dirt.ID
    # badgrass = grass[1:, :, :] & grass[:-1, :, :]
    #
    # section.Blocks[:-1, :, :][badgrass] = blocktypes.Dirt.ID
    #
    # # remove any thin snow layers immediately above other thin snow layers.
    # # minecraft doesn't flip out, but it's almost never intended
    # if hasattr(blocktypes, "SnowLayer"):
    #     snowlayer = section.Blocks == blocktypes.SnowLayer.ID
    #     badsnow = snowlayer[:, :, 1:] & snowlayer[:, :, :-1]
    #
    #     section.Blocks[:, :, 1:][badsnow] = 0

# --- Sections and chunks ---


class AnvilSection(object):
    """
    Internal representation of a 16x16x16 chunk section. Arrays are indexed YZX.

    4-bit arrays are unpacked to byte arrays to make them work with numpy's array routines.

    To create the full 12-bit block ID, the Blocks array is extended to 16 bits and the Add array is merged into
    the Blocks array.

    :ivar Y: section's Y value [0..(world.Height+15) >> 4]
    :ivar Blocks: Block IDs [0..4095]
    :ivar Data: Block sub-data [0..15]
    :ivar BlockLight: Light emitted by blocks [0..15]
    :ivar SkyLight: Light emitted by the sun/moon [0..15]
    """

    def __init__(self, section_tag=None):
        if section_tag:
            self._load(section_tag)
        else:
            self._create()

    def _load(self, section_tag):
        self.Y = section_tag.pop("Y").value
        self.Blocks = section_tag.pop("Blocks").value.astype("uint16")
        self.Blocks.shape = 16, 16, 16

        for name in "Data", "SkyLight", "BlockLight":
            section_array = section_tag.pop(name).value
            section_array.shape = 16, 16, 8
            setattr(self, name, unpackNibbleArray(section_array))

        tag = section_tag.pop("Add", None)
        if tag is not None:
            tag.value.shape = 16, 16, 8
            add = unpackNibbleArray(tag.value)
            self.Blocks |= numpy.array(add, 'uint16') << 8

        self.old_section_tag = section_tag

    def _create(self):
        shape = 16, 16, 16
        self.Y = 0

        self.Blocks = numpy.zeros(shape, 'uint16')
        self.Data = numpy.zeros(shape, 'uint8')
        self.SkyLight = numpy.zeros(shape, 'uint8')
        self.BlockLight = numpy.zeros(shape, 'uint8')
        self.old_section_tag = nbt.TAG_Compound()

    def buildNBTTag(self):
        """
        Return a TAG_Compound for saving this section to a chunk.
        """
        section_tag = self.old_section_tag

        Blocks = self.Blocks
        Data = packNibbleArray(self.Data)
        BlockLight = packNibbleArray(self.BlockLight)
        SkyLight = packNibbleArray(self.SkyLight)

        add = Blocks >> 8
        if add.any():
            section_tag["Add"] = nbt.TAG_Byte_Array(packNibbleArray(add).astype('uint8'))

        section_tag['Blocks'] = nbt.TAG_Byte_Array(numpy.array(Blocks, 'uint8'))
        section_tag['Data'] = nbt.TAG_Byte_Array(Data)
        section_tag['BlockLight'] = nbt.TAG_Byte_Array(BlockLight)
        section_tag['SkyLight'] = nbt.TAG_Byte_Array(SkyLight)

        section_tag["Y"] = nbt.TAG_Byte(self.Y)
        return section_tag


class AnvilChunkData(object):
    """ This is the chunk data backing a WorldEditorChunk. Chunk data is retained by the WorldEditor until its
    WorldEditorChunk is no longer used, then it is either cached in memory, discarded, or written to disk according to
    resource limits.
    """

    def __init__(self, adapter, cx, cz, dimName, rootTag=None, create=False):
        """

        :type adapter: mceditlib.anvil.adapter.AnvilWorldAdapter
        :type cx: int
        :type cz: int
        :type dimName: str
        :type rootTag: mceditlib.nbt.TAG_Compound
        :type create: bool
        :return:
        :rtype: AnvilChunkData
        """
        self.cx = cx
        self.cz = cz
        self.dimName = dimName
        self.adapter = adapter
        self.rootTag = rootTag
        self.dirty = False
        self._sections = {}

        if create:
            self._create()
        else:
            self._load(rootTag)

        levelTag = self.rootTag["Level"]
        if "Biomes" not in levelTag:
            levelTag["Biomes"] = nbt.TAG_Byte_Array(numpy.empty((16, 16), 'uint8'))
            levelTag["Biomes"].value[:] = -1

        self.Entities = [PCEntityRef(tag) for tag in self.rootTag["Level"]["Entities"]]
        del self.rootTag["Level"]["Entities"]
        self.TileEntities = [PCTileEntityRef(tag) for tag in self.rootTag["Level"]["TileEntities"]]
        del self.rootTag["Level"]["TileEntities"]

    def _create(self):
        chunkTag = nbt.TAG_Compound()
        chunkTag.name = ""

        levelTag = nbt.TAG_Compound()
        chunkTag["Level"] = levelTag

        levelTag["HeightMap"] = nbt.TAG_Int_Array(numpy.zeros((16, 16), 'uint32').newbyteorder())
        levelTag["TerrainPopulated"] = nbt.TAG_Byte(1)
        levelTag["xPos"] = nbt.TAG_Int(self.cx)
        levelTag["zPos"] = nbt.TAG_Int(self.cz)

        levelTag["LastUpdate"] = nbt.TAG_Long(0)

        levelTag["Entities"] = nbt.TAG_List()
        levelTag["TileEntities"] = nbt.TAG_List()

        self.rootTag = chunkTag

        self.dirty = True

    def _load(self, rootTag):
        self.rootTag = rootTag

        for sec in self.rootTag["Level"].pop("Sections", []):
            y = sec["Y"].value
            self._sections[y] = AnvilSection(sec)


    def buildNBTTag(self):
        """ does not recalculate any data or light """

        log.debug(u"Saving chunk: {0}".format(self))
        tag = self.rootTag.copy()

        sections = nbt.TAG_List()
        for _, section in self._sections.iteritems():

            if (not section.Blocks.any() and
                    not section.BlockLight.any() and
                    (section.SkyLight == 15).all()):
                continue

            sanitizeBlocks(section, self.adapter.blocktypes)
            sections.append(section.buildNBTTag())

        tag["Level"]["Sections"] = sections
        tag["Level"]["Entities"] = nbt.TAG_List([ref.rootTag for ref in self.Entities])
        tag["Level"]["TileEntities"] = nbt.TAG_List([ref.rootTag for ref in self.TileEntities])

        log.debug(u"Saved chunk {0}".format(self))
        return tag

    def sectionPositions(self):
        return self._sections.keys()

    def getSection(self, cy, create=False):
        """

        :param cy: Section number (y coordinate >> 4)
        :param create: If False, returns None if the section is not present, otherwise creates the section.
        :returns: The requested section, or None if it wasn't created.
        :rtype: AnvilSection
        :raises: ValueError if create is True and the requested section can't be stored in this chunk.
        """
        if (cy << 4) > self.adapter.maxHeight or cy < 0:
            if create:
                raise ValueError("Requested section %s exceeds world height" % cy)
            else:
                return None

        section = self._sections.get(cy)
        if not section:
            if not create:
                return None
            else:
                section = AnvilSection()
                section.Y = cy
                self._sections[cy] = section

        return section

    @property
    def bounds(self):
        return BoundingBox((self.cx << 4, self.adapter.minHeight, self.cz << 4),
                           (16, self.adapter.maxHeight - self.adapter.minHeight, 16))

    @property
    def blocktypes(self):
        return self.adapter.blocktypes

    @property
    def Biomes(self):
        return self.rootTag["Level"]["Biomes"].value.reshape((16, 16))

    @property
    def HeightMap(self):
        # z, x order in save file
        return self.rootTag["Level"]["HeightMap"].value.reshape((16, 16))

    @property
    def TerrainPopulated(self):
        return self.rootTag["Level"]["TerrainPopulated"].value

    @TerrainPopulated.setter
    def TerrainPopulated(self, val):
        """True or False. If False, the game will populate the chunk with
        ores and vegetation on next load"""
        self.rootTag["Level"]["TerrainPopulated"].value = val
        self.dirty = True

# --- World info ---


class AnvilWorldMetadata(object):

    def __init__(self, metadataTag):
        self.metadataTag = metadataTag
        self.rootTag = metadataTag["Data"]
        self.dirty = False

    # --- NBT Tag variables ---

    SizeOnDisk = nbtattr.NBTAttr('SizeOnDisk', nbt.TAG_Long, 0)
    RandomSeed = nbtattr.NBTAttr('RandomSeed', nbt.TAG_Long, 0)
    Time = nbtattr.NBTAttr('Time', nbt.TAG_Long, 0)  # Age of the world in ticks. 20 ticks per second; 24000 ticks per day.
    LastPlayed = nbtattr.NBTAttr('LastPlayed', nbt.TAG_Long, time.time() * 1000)

    LevelName = nbtattr.NBTAttr('LevelName', nbt.TAG_String, "Untitled World")

    MapFeatures = nbtattr.NBTAttr('MapFeatures', nbt.TAG_Byte, 1)

    GameType = nbtattr.NBTAttr('GameType', nbt.TAG_Int, 0)  # 0 for survival, 1 for creative

    version = nbtattr.NBTAttr('version', nbt.TAG_Int, VERSION_ANVIL)

    def worldSpawnPosition(self):
        return Vector(*[self.rootTag[i].value for i in ("SpawnX", "SpawnY", "SpawnZ")])

    def setWorldSpawnPosition(self, pos):
        for name, val in zip(("SpawnX", "SpawnY", "SpawnZ"), pos):
            self.rootTag[name] = nbt.TAG_Int(val)


class AnvilWorldAdapter(object):
    """
    Provides an interface to AnvilWorldFolder/RevisionHistory that is usable by WorldEditor

    This interface is the base used for all adapter classes. When writing a new adapter, make sure to
    implement all required methods and attributes. Required methods and attrs are the ones with docstrings.
    """

    minHeight = 0
    maxHeight = 256
    blocktypes = pc_blocktypes

    EntityRef = PCEntityRef
    TileEntityRef = PCTileEntityRef

    def __init__(self, filename=None, create=False, readonly=False, resume=None):
        """
        Load a Minecraft for PC level (Anvil format) from the given filename. It can point to either
        a level.dat or a folder containing one. If create is True, it will
        also create the world using a randomly selected seed.

        If you try to create an existing world, IOError will be raised.

        Uses a RevisionHistory to manage undo history. Upon creation, the world is read-only until createRevision() is
        called. Call createRevision() to create a new revision, or selectRevision() to revert to an earlier
        revision. Older revisions are read-only, so createRevision() must be called again to make further changes.

        Call writeAllChanges() to write all changes into the original world.

        :type filename: str or unicode
        :type create: bool
        :type readonly: bool
        :rtype: AnvilWorldAdapter
        """
        self.lockTime = 0

        assert not (create and readonly)

        if os.path.basename(filename) in ("level.dat", "level.dat_old"):
            filename = os.path.dirname(filename)

        if not os.path.exists(filename):
            if not create:
                raise IOError('File not found')

            os.mkdir(filename)
        else:
            if create:
                if not os.path.isdir(filename) or os.path.exists(os.path.join(filename, "level.dat")):
                    raise IOError('File exists!')

        if not os.path.isdir(filename):
            raise IOError('File is not a Minecraft Anvil world')

        if readonly:
            self.revisionHistory = AnvilWorldFolder(filename)
            self.selectedRevision = self.revisionHistory
        else:
            self.revisionHistory = RevisionHistory(filename, resume)
            self.selectedRevision = self.revisionHistory.getHead()

        self.filename = filename
        self.readonly = readonly
        if not readonly:
            self.acquireSessionLock()

        if create:
            self._createMetadataTag()
            self.selectedRevision.writeFile("level.dat", self.metadata.metadataTag.save())

        else:
            try:
                metadataTag = nbt.load(buf=self.selectedRevision.readFile("level.dat"))
                self.metadata = AnvilWorldMetadata(metadataTag)
            except (EnvironmentError, zlib.error) as e:
                log.info("Error loading level.dat, trying level.dat_old ({0})".format(e))
                try:
                    metadataTag = nbt.load(buf=self.selectedRevision.readFile("level.dat_old"))
                    self.metadata = AnvilWorldMetadata(metadataTag)
                    log.info("level.dat restored from backup.")
                    self.saveChanges()
                except Exception as e:
                    traceback.print_exc()
                    log.info("%r while loading level.dat_old. Initializing with defaults.", e)
                    self._createMetadataTag()

        assert self.metadata.version == VERSION_ANVIL, "Pre-Anvil world formats are not supported (for now)"

    def __repr__(self):
        return "AnvilWorldAdapter(%r)" % self.filename

    # --- Create, save, close ---

    def _createMetadataTag(self, random_seed=None):
        """
        Create a level.dat for a newly created world or a world found with damaged level.dat/.dat_old (xxx repair in
        WorldEditor?)
        :param random_seed:
        :type random_seed:
        :return:
        :rtype:
        """
        metadataTag = nbt.TAG_Compound()
        metadataTag["Data"] = nbt.TAG_Compound()
        metadataTag["Data"]["SpawnX"] = nbt.TAG_Int(0)
        metadataTag["Data"]["SpawnY"] = nbt.TAG_Int(2)
        metadataTag["Data"]["SpawnZ"] = nbt.TAG_Int(0)

        last_played = long(time.time() * 1000)
        if random_seed is None:
            random_seed = long(random.random() * 0xffffffffffffffffL) - 0x8000000000000000L

        metadataTag["Data"]['version'] = nbt.TAG_Int(VERSION_ANVIL)

        self.metadata = AnvilWorldMetadata(metadataTag)

        self.metadata.LastPlayed = long(last_played)
        self.metadata.RandomSeed = long(random_seed)
        self.metadata.SizeOnDisk = 0
        self.metadata.Time = 1
        self.metadata.LevelName = os.path.basename(self.filename)

    def syncToDisk(self):
        """
        Write cached items (metadata from level.dat and players in players/ folder) to the current revision.
        :return:
        :rtype:
        """
        if self.metadata.dirty:
            self.selectedRevision.writeFile("level.dat", self.metadata.metadataTag.save())
            self.metadata.dirty = False

    def saveChanges(self):
        """
        Write all changes from all revisions into the world folder.

        :return:
        :rtype: None
        """
        if self.readonly:
            raise IOError("World is opened read only.")

        self.checkSessionLock()
        self.revisionHistory.writeAllChanges(self.selectedRevision)
        self.selectedRevision = self.revisionHistory.getHead()

    def close(self):
        """
        Close the world, deleting temporary files and freeing resources. Operations on a closed world are undefined.

        :return:
        :rtype: None
        """
        self.revisionHistory.close()
        pass  # do what here???

    # --- Undo revisions ---

    def requireRevisions(self):
        """
        Enforce the creation of new revisions by making the world folder's revision read-only.
        :return:
        :rtype:
        """
        self.revisionHistory.rootNode.readOnly = True

    def createRevision(self):
        """
        Create a new undo revision. Subsequent changes should be stored in the new revision.

        :return:
        :rtype:
        """
        self.selectedRevision = self.revisionHistory.createRevision(self.selectedRevision)

    def closeRevision(self):
        """
        Close the current revision and mark it read-only. Subsequent edits will not be possible until createRevision
        is called.

        :return:
        :rtype:
        """
        self.revisionHistory.closeRevision()

    def setRevisionInfo(self, info):
        """
        Attach some arbitrary JSON-serializable data to the current revision

        :param info: JSON-serializable data
        :type info: list | dict | str | unicode | int
        """
        self.selectedRevision.setRevisionInfo(info)

    def getRevisionInfo(self):
        """
        Return JSON-serializable data attached previously to the current revision via setRevisionInfo, or None if
        no data is attached.

        :return:
        :rtype: list | dict | str | unicode | int | None
        """
        return self.selectedRevision.getRevisionInfo()

    def selectRevision(self, index):
        """
        Select the current revision by index. Return changes between the previous revision and this one. If
         the index is invalid, returns None and does nothing.

         (XXX use an ID instead of index?)

        :param index:
        :type index:
        :return:
        :rtype: RevisionChanges
        """
        if index < 0 or index >= len(self.revisionHistory.nodes):
            return None
        newRevision = self.revisionHistory.getRevision(index)
        changes = self.revisionHistory.getRevisionChanges(self.selectedRevision, newRevision)
        self.selectedRevision = newRevision
        return changes

    def listRevisions(self):
        """
        List the revision indexes and infos as (index, info) tuples. Info is JSON-serializable data previously attached
        with setRevisionInfo, or None.

        :return:
        :rtype: iterator[(int, list | dict | str | unicode | int)]
        """
        for ID, node in enumerate(self.revisionHistory.nodes):
            yield ID, node.getRevisionInfo()

    # --- Session lock ---

    def acquireSessionLock(self):
        """
        Acquire the world's session.lock. Formats without this file may do nothing.

        :return:
        :rtype:
        """
        lockfile = self.revisionHistory.rootFolder.getFilePath("session.lock")
        self.lockTime = int(time.time() * 1000)
        with file(lockfile, "wb") as f:
            f.write(struct.pack(">q", self.lockTime))
            f.flush()
            os.fsync(f.fileno())

    def checkSessionLock(self):
        """
        Make sure the lock previously acquired by acquireSessionLock is still valid. Raise SessionLockLost if it is
        not. Raising the exception will abort any writes done to the main world folder.

        :return:
        :rtype:
        """
        if self.readonly:
            raise SessionLockLost("World is opened read only.")

        lockfile = self.revisionHistory.rootFolder.getFilePath("session.lock")
        try:
            (lock, ) = struct.unpack(">q", file(lockfile, "rb").read())
        except struct.error:
            lock = -1
        if lock != self.lockTime:
            raise SessionLockLost("Session lock lost. This world is being accessed from another location.")

    # --- Format detection ---

    @classmethod
    def canOpenFile(cls, filename):
        """
        Ask this adapter if it can open the given file.

        :param filename: File to identify
        :type filename: str | unicode
        :return:
        :rtype: boolean
        """
        if os.path.exists(os.path.join(filename, "chunks.dat")):
            return False  # exclude Pocket Edition folders

        if not os.path.isdir(filename):
            f = os.path.basename(filename)
            if f not in ("level.dat", "level.dat_old"):
                return False
            filename = os.path.dirname(filename)

        files = os.listdir(filename)
        if "level.dat" in files or "level.dat_old" in files:
            return True

        return False

    # --- Dimensions ---

    def listDimensions(self):
        """
        List the names of all dimensions in this world.

        :return:
        :rtype: iterator of str
        """
        return self.selectedRevision.listDimensions()

    # --- Chunks ---

    def chunkCount(self, dimName):
        """
        Count the chunks in the given dimension

        :param dimName:
        :type dimName: str
        :return:
        :rtype: int
        """
        return self.selectedRevision.chunkCount(dimName)

    def chunkPositions(self, dimName):
        """
        List the chunk positions (cx, cz) in the given dimension.

        :type dimName: unicode or str
        :return:
        :rtype: Iterator of (int, int)
        """
        return iter(self.selectedRevision.chunkPositions(dimName))

    def containsChunk(self, cx, cz, dimName):
        """
        Return whether the given chunk is present in the given dimension

        :type cx: int or dtype
        :type cz: int or dtype
        :type dimName: str
        :return:
        :rtype: bool
        """
        return self.selectedRevision.containsChunk(cx, cz, dimName)

    def readChunk(self, cx, cz, dimName):
        """
        Return chunk (cx, cz) in the given dimension as an AnvilChunkData. Raise ChunkNotPresent if not found.

        :type cx: int or dtype
        :type cz: int or dtype
        :type dimName: str
        :return:
        :rtype: AnvilChunkData
        """
        try:
            data = self.selectedRevision.readChunkBytes(cx, cz, dimName)
            chunkTag = nbt.load(buf=data)
            log.debug("_getChunkData: Chunk %s loaded (%s bytes)", (cx, cz), len(data))
            chunkData = AnvilChunkData(self, cx, cz, dimName, chunkTag)

        except (KeyError, IndexError, zlib.error) as e:  # Missing nbt keys, lists too short, decompression failure
            raise AnvilChunkFormatError("Error loading chunk: %r" % e)

        return chunkData

    def writeChunk(self, chunk):
        """
        Write the given AnvilChunkData to the current revision.

        :type chunk: mceditlib.anvil.adapter.AnvilChunkData
        """
        tag = chunk.buildNBTTag()
        self.selectedRevision.writeChunkBytes(chunk.cx, chunk.cz, chunk.dimName, tag.save(compressed=False))

    def createChunk(self, cx, cz, dimName):
        """
        Create a new empty chunk at the given position in the given dimension.

        :type cx: int
        :type cz: int
        :type dimName: str
        :return:
        :rtype: AnvilChunkData
        """
        if self.selectedRevision.containsChunk(cx, cz, dimName):
            raise ValueError("Chunk %s already exists in dim %s", (cx, cz), dimName)
        chunk = AnvilChunkData(self, cx, cz, dimName, create=True)
        self.selectedRevision.writeChunkBytes(cx, cz, dimName, chunk.buildNBTTag().save(compressed=False))
        return chunk

    def deleteChunk(self, cx, cz, dimName):
        """
        Delete the chunk at the given position in the given dimension.

        :type cx: int
        :type cz: int
        :type dimName: str
        """
        self.selectedRevision.deleteChunk(cx, cz, dimName)

    # --- Players ---

    def listPlayers(self):
        """
        List the names of all players in this world (XXX players folder in dimension folders??)

        :return:
        :rtype: Iterator of [str]
        """
        for f in self.selectedRevision.listFolder("playerdata"):
            if f.endswith(".dat"):
                yield f[11:-4]

        if "Player" in self.metadata.rootTag:
            yield ""

    def getPlayer(self, playerUUID=""):
        return AnvilPlayerRef(self, playerUUID)

    def getPlayerTag(self, playerUUID=""):
        """
        Return the root NBT tag for the named player. Raise PlayerNotFound if not present.

        :param playerUUID:
        :type playerUUID: unicode
        :return:
        :rtype: PCPlayer
        """
        if playerUUID == "":
            if "Player" in self.metadata.rootTag:
                # single-player world
                playerTag = self.metadata.rootTag["Player"]
                return playerTag
            raise PlayerNotFound(playerUUID)
        else:
            playerFilePath = "playerdata/%s.dat" % playerUUID
            if self.selectedRevision.containsFile(playerFilePath):
                # multiplayer world, found this player
                playerTag = nbt.load(buf=self.selectedRevision.readFile(playerFilePath))
                return playerTag
            else:
                raise PlayerNotFound(playerUUID)

    def savePlayerTag(self, tag, playerUUID):
        if playerUUID == "":
            # sync metadata?
            self.metadata.dirty = True
        else:
            self.selectedRevision.writeFile("playerdata/%s.dat" % playerUUID, tag.save())

    def createPlayer(self, playerUUID=""):
        """
        Create a new player with the given name and return the PlayerRef. Raises some kind of IOError if the player
         could not be created.

        :param playerUUID:
        :type playerUUID: str
        :return:
        :rtype: PCPlayer
        """
        if self.readonly:
            raise IOError("World is opened read only.")

        playerFilePath = "playerdata/%s.dat" % playerUUID

        if playerUUID == "":
            if "Player" in self.metadata.rootTag["Data"]:
                raise IOError("Single-player player already exists.")
            playerTag = nbt.TAG_Compound()
            self.metadata.rootTag["Data"]["Player"] = playerTag
        else:
            if self.selectedRevision.containsFile(playerFilePath):
                raise ValueError("Cannot create player %s: already exists.")

            playerTag = nbt.TAG_Compound()

        player = AnvilPlayerRef(playerTag, self)
        nbtattr.SetNBTDefaults(player)

        if playerUUID != "Player":
            self.checkSessionLock()
            self.selectedRevision.writeFile(playerFilePath, playerTag.save())

        return self.getPlayer(playerUUID)


class PlayerAbilitiesAttrs(nbtattr.CompoundAttrs):
    mayBuild = nbtattr.NBTAttr('mayBuild', nbt.TAG_Byte, 0)
    instabuild = nbtattr.NBTAttr('instabuild', nbt.TAG_Byte, 0)
    flying = nbtattr.NBTAttr('flying', nbt.TAG_Byte, 0)
    mayfly = nbtattr.NBTAttr('mayfly', nbt.TAG_Byte, 0)
    invulnerable = nbtattr.NBTAttr('invulnerable', nbt.TAG_Byte, 0)


class AnvilPlayerRef(object):
    def __init__(self, adapter, playerUUID):
        self.playerUUID = playerUUID
        self.adapter = adapter
        self.rootTag = adapter.getPlayerTag(playerUUID)
        self.dirty = False
    #
    # @property
    # def rootTag(self):
    #     if self.playerTag is None or self.playerTag() is None:
    #         tag = self.adapter.getPlayerTag(self.playerName)
    #         self.playerTag = weakref.ref(tag)
    #
    #         return tag
    #     return self.playerTag()

    UUID = nbtattr.NBTUUIDAttr()

    id = nbtattr.NBTAttr("id", nbt.TAG_String)
    Position = nbtattr.NBTVectorAttr("Pos", nbt.TAG_Double)
    Motion = nbtattr.NBTVectorAttr("Motion", nbt.TAG_Double)
    Rotation = nbtattr.NBTListAttr("Rotation", nbt.TAG_Float)

    Air = nbtattr.NBTAttr('Air', nbt.TAG_Short, 300)
    AttackTime = nbtattr.NBTAttr('AttackTime', nbt.TAG_Short, 0)
    DeathTime = nbtattr.NBTAttr('DeathTime', nbt.TAG_Short, 0)
    Fire = nbtattr.NBTAttr('Fire', nbt.TAG_Short, -20)
    Health = nbtattr.NBTAttr('Health', nbt.TAG_Short, 20)
    HurtTime = nbtattr.NBTAttr('HurtTime', nbt.TAG_Short, 0)
    Score = nbtattr.NBTAttr('Score', nbt.TAG_Int, 0)
    FallDistance = nbtattr.NBTAttr('FallDistance', nbt.TAG_Float, 0)
    OnGround = nbtattr.NBTAttr('OnGround', nbt.TAG_Byte, 0)
    Dimension = nbtattr.NBTAttr('OnGround', nbt.TAG_Int, 0)

    Inventory = nbtattr.NBTListAttr('Inventory', nbt.TAG_Compound)

    GAMETYPE_SURVIVAL = 0
    GAMETYPE_CREATIVE = 1
    GAMETYPE_ADVENTURE = 2
    GameType = nbtattr.NBTAttr('playerGameType', nbt.TAG_Int, GAMETYPE_SURVIVAL)

    abilities = nbtattr.NBTCompoundAttr("abilities", PlayerAbilitiesAttrs)

    def setAbilities(self, gametype):
        # Assumes GAMETYPE_CREATIVE is the only mode with these abilities set,
        # which is true for now.  Future game modes may not hold this to be
        # true, however.
        if gametype == self.GAMETYPE_CREATIVE:
            self.abilities.instabuild = True
            self.abilities.mayfly = True
            self.abilities.invulnerable = True
        else:
            self.abilities.flying = True
            self.abilities.instabuild = True
            self.abilities.mayfly = True
            self.abilities.invulnerable = True

    def setGameType(self, gametype):
        self.GameType = gametype
        self.setAbilities(gametype)

    @property
    def Spawn(self):
        return [self.rootTag[i].value for i in ("SpawnX", "SpawnY", "SpawnZ")]

    @Spawn.setter
    def Spawn(self, pos):
        for name, val in zip(("SpawnX", "SpawnY", "SpawnZ"), pos):
            self.rootTag[name] = nbt.TAG_Int(val)

    def save(self):
        if self.dirty:
            self.adapter.savePlayerTag(self.rootTag, self.playerUUID)
            self.dirty = False

    _dimNames = {
        -1:"DIM-1",
        0:"",
        1:"DIM1",
        }

    _dimNumbers = {v:k for k, v in _dimNames.iteritems()}

    @property
    def dimName(self):
        return self._dimNames[self.Dimension]

    @dimName.setter
    def dimName(self, name):
        self.Dimension = self._dimNumbers[name]
