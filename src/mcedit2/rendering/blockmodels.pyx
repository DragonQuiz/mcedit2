#cython: boundscheck=False
"""
    blockmodels
"""
from __future__ import absolute_import, print_function
import json
import logging
import math

import numpy
cimport numpy

from cpython cimport array
from array import array

from mceditlib import faces
from mceditlib.geometry import Vector, FloatBox

from libc.stdlib cimport malloc, free
from libc.string cimport memset

log = logging.getLogger(__name__)

cdef struct ModelQuad:
    float[24] xyzuvc
    char[4] cullface  # isCulled, dx, dy, dz

cdef struct ModelQuadList:
    int count
    ModelQuad *quads

cdef class BlockModels(object):

    def _getBlockModel(self, modelName):
        model = self.modelBlockJsons.get(modelName)
        if model is None:
            model = json.load(self.resourceLoader.openStream("models/%s.json" % modelName))
            self.modelBlockJsons[modelName] = model
        return model

    def _getBlockState(self, stateName):
        state = self.modelStateJsons.get(stateName)
        if state is None:
            state = json.load(self.resourceLoader.openStream("blockstates/%s.json" % stateName))
            self.modelStateJsons[stateName] = state
        return state

    def __init__(self, blocktypes, resourceLoader):
        """

        :param blocktypes:
        :type blocktypes: mceditlib.blocktypes.BlockTypeSet
        :param resourceLoader:
        :type resourceLoader: ResourceLoader
        :return:
        :rtype: BlockModels
        """
        self.resourceLoader = resourceLoader
        self.blocktypes = blocktypes

        self.modelBlockJsons = {}
        self.modelStateJsons = {}
        self.modelQuads = {}
        self._textureNames = set()
        self.firstTextures = {}  # first texture found for each block - used for icons (xxx)
        self.cookedModels = {}  # nameAndState -> list[(xyzuvc, cullface)]
        #self.cookedModelsByID = numpy.zeros((256*16, 16), dtype=list)  # (id, meta) -> list[(xyzuvc, cullface)]
        memset(self.cookedModelsByID, 0, sizeof(self.cookedModelsByID))
        self.cooked = False

        for i, block in enumerate(blocktypes):
            if i % 100 == 0:
                log.info("Loading block models %s/%s", i, len(blocktypes))

            if block.renderType != 3:  # only rendertype 3 uses block models
                continue
            nameAndState = block.internalName + block.blockState
            try:
                statesJson = self._getBlockState(block.resourcePath)
            except KeyError:
                log.warn("Could not get blockstates resource for %s, skipping...", block)
                continue
            variants = statesJson['variants']
            # variants is a dict with each key a resourceVariant value (from the block's ModelResourceLocation)
            # the value for this key is either a dict describing which model to use
            # ... or a list of such models to be selected from randomly
            #
            # each model dict must have a 'model' key whose value is the name of a file under assets/minecraft/models
            # model dict may also have optional keys 'x', 'y', 'z' with a value in degrees, to rotate the model
            # around that axis
            # another optional key is 'uvlock', which needs investigating
            # variant dict for 'rail':

            # "variants": {
            #     "shape=north_south": { "model": "normal_rail_flat" },
            #     "shape=east_west": { "model": "normal_rail_flat", "y": 90 },
            #     "shape=ascending_east": { "model": "normal_rail_raised_ne", "y": 90 },
            #     "shape=ascending_west": { "model": "normal_rail_raised_sw", "y": 90 },
            #     "shape=ascending_north": { "model": "normal_rail_raised_ne" },
            #     "shape=ascending_south": { "model": "normal_rail_raised_sw" },
            #     "shape=south_east": { "model": "normal_rail_curved" },
            #     "shape=south_west": { "model": "normal_rail_curved", "y": 90 },
            #     "shape=north_west": { "model": "normal_rail_curved", "y": 180 },
            #     "shape=north_east": { "model": "normal_rail_curved", "y": 270 }
            # }

            variantBlockState = block.resourceVariant
            log.debug("Loading %s#%s for %s", block.resourcePath, block.resourceVariant, block)
            variantDict = variants[variantBlockState]
            if isinstance(variantDict, list):
                variantDict = variantDict[0]  # do the random pick thing later, if at all
            modelName = variantDict['model']
            try:
                modelDict = self._getBlockModel("block/" + modelName)
            except ValueError as e:
                log.exception("Error parsing json for block/%s: %s", modelName, e)
                continue
            variantXrot = variantDict.get("x", 0)
            variantYrot = variantDict.get("y", 0)
            variantZrot = variantDict.get("z", 0)

            # model will either have an 'elements' key or a 'parent' key (maybe both).
            # 'parent' will be the name of a model
            # following 'parent' keys will eventually lead to a model with 'elements'
            #
            # 'elements' is a list of dicts each describing a box that makes up the model.
            # each box dict has 'from' and 'to' keys, which are lists of 3 float coordinates.
            #
            # the 'crossed squares' model demonstrates most of the keys found in a box element
            #
            # {   "from": [ 0.8, 0, 8 ],
            #     "to": [ 15.2, 16, 8 ],
            #     "rotation": { "origin": [ 8, 8, 8 ], "axis": "y", "angle": 45, "rescale": true },
            #     "shade": false,
            #     "faces": {
            #         "north": { "uv": [ 0, 0, 16, 16 ], "texture": "#cross" },
            #         "south": { "uv": [ 0, 0, 16, 16 ], "texture": "#cross" }
            #     }
            # }
            #
            # model may also have a 'textures' dict which assigns a texture file to a texture variable,
            # or a texture variable to another texture variable.
            #
            # the result of loading a model should be a list of quads, each with four vertexes, four pairs of
            # texture coordinates, four RGBA values for shading, plus a Face telling which adjacent block when
            # present causes that quad to be culled.

            textureVars = {}
            allElements = []

            # grab textures and elements from this model, then get parent and merge its textures and elements
            # continue until no parent is found
            for i in range(30):
                textures = modelDict.get("textures")
                if textures is not None:
                    textureVars.update(textures)
                elements = modelDict.get("elements")
                if elements is not None:
                    allElements.extend(elements)
                parentName = modelDict.get("parent")
                if parentName is None:
                    break
                try:
                    modelDict = self._getBlockModel(parentName)
                except ValueError as e:
                    log.exception("Error parsing json for block/%s: %s", parentName, e)
                    raise
            else:
                raise ValueError("Parent loop detected in block model %s" % modelName)

            try:
                # each element describes a box with up to six faces, each with a texture. convert the box into
                # quads.
                allQuads = []

                if block.internalName == "minecraft:redstone_wire":
                    blockColor = (0xff, 0x33, 0x00)
                else:
                    blockColor = block.color
                    r = (blockColor >> 16) & 0xff
                    g = (blockColor >> 8) & 0xff
                    b = blockColor & 0xff
                    blockColor = r, g, b

                for element in allElements:
                    quads = self.buildBoxQuads(element, nameAndState, textureVars, variantXrot, variantYrot, variantZrot, blockColor)
                    allQuads.extend(quads)



                self.modelQuads[block.internalName + block.blockState] = allQuads

            except Exception as e:
                log.error("Failed to parse variant of block %s\nelements:\n%s\ntextures:\n%s", nameAndState,
                          allElements, textureVars)
                raise

    def buildBoxQuads(self, element, nameAndState, textureVars, variantXrot, variantYrot, variantZrot, blockColor):
        quads = []
        shade = element.get("shade", True)
        fromPoint = Vector(*element["from"])
        toPoint = Vector(*element["to"])
        fromPoint /= 16.
        toPoint /= 16.
        box = FloatBox(fromPoint, maximum=toPoint)
        for face, info in element["faces"].iteritems():
            face = facesByCardinal[face]
            texture = info["texture"]
            cullface = info.get("cullface")

            uv = info.get("uv", [0, 0, 16, 16])

            lastvar = texture

            tintindex = info.get("tintindex")
            if tintindex is not None:
                tintcolor = blockColor
            else:
                tintcolor = None

            # resolve texture variables
            for i in range(30):
                if texture is None:
                    raise ValueError("Texture variable %s is not assigned." % lastvar)
                elif texture[0] == "#":
                    lastvar = texture
                    texture = textureVars[texture[1:]]
                else:
                    break
            else:
                raise ValueError("Texture variable loop detected!")

            self.firstTextures.setdefault(nameAndState, texture)
            self._textureNames.add(texture)

            quads.append((box, face,
                    texture, uv, cullface,
                    shade, element.get("rotation"), info.get("rotation"),
                    variantXrot, variantYrot, variantZrot, tintcolor))

        return quads

    def getTextureNames(self):
        return iter(self._textureNames)

    def cookQuads(self, textureAtlas):
        if self.cooked:
            return
        log.info("Cooking quads for %d models...", len(self.modelQuads))
        cookedModels = {}
        cdef int l, t, w, h
        cdef int u1, u2, v1, v2
        cdef int uw, vh
        cdef list cookedQuads
        for nameAndState, allQuads in self.modelQuads.iteritems():
            cookedQuads = []
            for (box, face, texture, uv, cullface, shade, rotation, textureRotation,
                 variantXrot, variantYrot, variantZrot, tintcolor) in allQuads:

                l, t, w, h = textureAtlas.texCoordsByName[texture]
                u1, v1, u2, v2 = uv
                uw = (w * (u2 - u1)) / 16
                vh = (w * (v2 - v1)) / 16  # w is assumed to be the height of a single frame in an animation xxxxx read .mcmeta
                u1 += l
                u2 = u1 + uw

                # flip v axis - texcoords origin is top left but model uv origin is from bottom left
                v1 = t + h - v1
                v2 = v1 - vh

                uv = (u1, v1, u2, v2)

                xyzuvc = getBlockFaceVertices(box, face, uv, textureRotation)
                xyzuvc.shape = 4, 6

                if variantZrot:
                    face = rotateFace(face, 2, variantZrot)
                if variantXrot:
                    face = rotateFace(face, 0, variantXrot)
                if variantYrot:
                    face = rotateFace(face, 1, variantYrot)
                if cullface:
                    cullface = facesByCardinal[cullface]
                    if variantZrot:
                        cullface = rotateFace(cullface, 2, variantZrot)
                    if variantXrot:
                        cullface = rotateFace(cullface, 0, variantXrot)
                    if variantYrot:
                        cullface = rotateFace(cullface, 1, variantYrot)

                self.rotateVertices(rotation, variantXrot, variantYrot, variantZrot, xyzuvc)

                rgba = xyzuvc.view('uint8')[:, 20:]
                if shade:
                    rgba[:] = faceShades[face]
                else:
                    rgba[:] = 0xff

                if tintcolor is not None:
                    rgba[..., 0] = (tintcolor[0] * int(rgba[0, 0])) >> 8
                    rgba[..., 1] = (tintcolor[1] * int(rgba[0, 1])) >> 8
                    rgba[..., 2] = (tintcolor[2] * int(rgba[0, 2])) >> 8

                xyzuvc.shape = 24  # flatten to store in ModelQuad.xyzuvc

                cookedQuads.append((xyzuvc, cullface))

            cookedModels[nameAndState] = cookedQuads
            ID, meta = self.blocktypes.IDsByState[nameAndState]
            self.storeQuads(cookedQuads, ID, meta)

        self.cookedModels = cookedModels
        self.cooked = True

    def storeQuads(self, list cookedQuads, unsigned short ID, unsigned char meta):
        cdef ModelQuadList modelQuads
        modelQuads.count = len(cookedQuads)
        cdef void * quads = malloc(modelQuads.count * sizeof(ModelQuad))
        modelQuads.quads = <ModelQuad *>quads
        cdef float[:] xyzuvc, quadxyzuvc
        cdef int i
        for i in range(modelQuads.count):
            xyzuvc, cullface = cookedQuads[i]
            quadxyzuvc = modelQuads.quads[i].xyzuvc
            quadxyzuvc[:] = xyzuvc[:]
            if cullface is not None:
                modelQuads.quads[i].cullface[0] = 1
                dx, dy, dz = cullface.vector
                modelQuads.quads[i].cullface[1] = dx
                modelQuads.quads[i].cullface[2] = dy
                modelQuads.quads[i].cullface[3] = dz
            else:
                modelQuads.quads[i].cullface[0] = 0
                modelQuads.quads[i].cullface[1] = 0
                modelQuads.quads[i].cullface[2] = 0
                modelQuads.quads[i].cullface[3] = 0



        self.cookedModelsByID[ID][meta] = modelQuads

    def rotateVertices(self, rotation, variantXrot, variantYrot, variantZrot, xyzuvc):
        if rotation is not None:
            origin = rotation["origin"]
            axis = rotation["axis"]
            angle = rotation["angle"]
            rescale = rotation.get("rescale", False)
            matrix = npRotate(axis, angle, rescale)
            ox, oy, oz = origin
            origin = ox / 16., oy / 16., oz / 16.

            xyzuvc[:, :3] -= origin
            xyz = xyzuvc[:, :3].transpose()
            xyzuvc[:, :3] = (matrix[:3, :3] * xyz).transpose()
            xyzuvc[:, :3] += origin
        rotate = variantXrot or variantYrot or variantZrot
        if rotate:
            matrix = numpy.matrix(numpy.identity(4))
            if variantYrot:
                matrix *= npRotate("y", -variantYrot)
            if variantXrot:
                matrix *= npRotate("x", -variantXrot)
            if variantZrot:
                matrix *= npRotate("z", -variantZrot)
            xyzuvc[:, :3] -= 0.5, 0.5, 0.5
            xyz = xyzuvc[:, :3].transpose()
            xyzuvc[:, :3] = (matrix[:3, :3] * xyz).transpose()
            xyzuvc[:, :3] += 0.5, 0.5, 0.5

faceRotations = (
    (
        faces.FaceYIncreasing,
        faces.FaceZIncreasing,
        faces.FaceYDecreasing,
        faces.FaceZDecreasing,
    ),
    (
        faces.FaceXIncreasing,
        faces.FaceZDecreasing,
        faces.FaceXDecreasing,
        faces.FaceZIncreasing,
    ),
    (
        faces.FaceXIncreasing,
        faces.FaceYIncreasing,
        faces.FaceXDecreasing,
        faces.FaceYDecreasing,
    ),

)

def rotateFace(face, axis, degrees):
    rots = faceRotations[axis]
    try:
        idx = rots.index(face)
    except ValueError:
        return face

    while degrees > 0:
        idx -= 1
        degrees -= 90
    idx %= 4
    return rots[idx]


def npRotate(axis, angle, rescale=False):
    # ( xx(1-c)+c	xy(1-c)-zs  xz(1-c)+ys	 0  )
    # | yx(1-c)+zs	yy(1-c)+c   yz(1-c)-xs	 0  |
    # | xz(1-c)-ys	yz(1-c)+xs  zz(1-c)+c	 0  |
    # (	 0	        0		    0	         1  )
    # axis:
    # "x": (1, 0, 0)
    # "y": (0, 1, 0)
    # "z": (0, 0, 1)
    x = y = z = 0
    if axis == "x":
        x = 1
    elif axis == "y":
        y = 1
    elif axis == "z":
        z = 1
    else:
        raise ValueError("Unknown axis: %r" % axis)

    s = math.sin(math.radians(angle))
    c = math.cos(math.radians(angle))
    rotate = numpy.matrix([[x*x*(1-c)+c,    x*y*(1-c)-z*s,  x*z*(1-c)+y*s,  0],
                           [y*x*(1-c)+z*s,  y*y*(1-c)+c,    y*z*(1-c)-x*s,  0],
                           [x*z*(1-c)-y*s,  y*z*(1-c)+x*s,  z*z*(1-c)+c,    0],
                           [0,              0,              0,              1]])
    # xxx rescale
    return rotate


facesByCardinal = dict(
    north=faces.FaceNorth,
    south=faces.FaceSouth,
    east=faces.FaceEast,
    west=faces.FaceWest,
    up=faces.FaceUp,
    down=faces.FaceDown,

)

faceShades = {
    faces.FaceNorth: 0x99,
    faces.FaceSouth: 0x99,
    faces.FaceEast: 0xCC,
    faces.FaceWest: 0xCC,
    faces.FaceUp: 0xFF,
    faces.FaceDown: 0x77,
}


cdef getBlockFaceVertices(box, face, tuple uv, textureRotation):
    cdef float x1, y1, z1, x2, y2, z2,
    cdef int u1, v1, u2, v2
    x1, y1, z1 = box.origin
    x2, y2, z2 = box.maximum
    u1, v1, u2, v2 = uv
    tc = [
        (u1, v1),
        (u1, v2),
        (u2, v2),
        (u2, v1),
    ]
    if textureRotation:
        roll = textureRotation / 90
        roll %= 4
        tc = tc[roll:] + tc[:roll]
    tc = numpy.array(tc)

    if face == faces.FaceXDecreasing:
        faceVertices = numpy.array(
            (x1, y2, z1, 0.0, 0.0, 0.0,
             x1, y1, z1, 0.0, 0.0, 0.0,
             x1, y1, z2, 0.0, 0.0, 0.0,
             x1, y2, z2, 0.0, 0.0, 0.0,
             ), dtype='f4')

    elif face == faces.FaceXIncreasing:
        faceVertices = numpy.array(
            (x2, y2, z2, 0.0, 0.0, 0.0,
             x2, y1, z2, 0.0, 0.0, 0.0,
             x2, y1, z1, 0.0, 0.0, 0.0,
             x2, y2, z1, 0.0, 0.0, 0.0,
             ), dtype='f4')

    elif face == faces.FaceYDecreasing:
        faceVertices = numpy.array(
            (x1, y1, z2, 0.0, 0.0, 0.0,
             x1, y1, z1, 0.0, 0.0, 0.0,
             x2, y1, z1, 0.0, 0.0, 0.0,
             x2, y1, z2, 0.0, 0.0, 0.0,
             ), dtype='f4')

    elif face == faces.FaceYIncreasing:
        faceVertices = numpy.array(
            (x1, y2, z1, 0.0, 0.0, 0.0,
             x1, y2, z2, 0.0, 0.0, 0.0,
             x2, y2, z2, 0.0, 0.0, 0.0,
             x2, y2, z1, 0.0, 0.0, 0.0,
             ), dtype='f4')

    elif face == faces.FaceZDecreasing:
        faceVertices = numpy.array(
            (x2, y2, z1, 0.0, 0.0, 0.0,
             x2, y1, z1, 0.0, 0.0, 0.0,
             x1, y1, z1, 0.0, 0.0, 0.0,
             x1, y2, z1, 0.0, 0.0, 0.0,
             ), dtype='f4')

    elif face == faces.FaceZIncreasing:
        faceVertices = numpy.array(
            (x1, y2, z2, 0.0, 0.0, 0.0,
             x1, y1, z2, 0.0, 0.0, 0.0,
             x2, y1, z2, 0.0, 0.0, 0.0,
             x2, y2, z2, 0.0, 0.0, 0.0,
             ), dtype='f4')
    else:
        raise ValueError("Unknown face %s" % face)

    faceVertices.shape = 4, 6
    faceVertices[:, 3:5] = tc

    return faceVertices
