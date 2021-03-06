#    This file is part of the Minecraft Overviewer.
#
#    Minecraft Overviewer is free software: you can redistribute it and/or
#    modify it under the terms of the GNU General Public License as published
#    by the Free Software Foundation, either version 3 of the License, or (at
#    your option) any later version.
#
#    Minecraft Overviewer is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General
#    Public License for more details.
#
#    You should have received a copy of the GNU General Public License along
#    with the Overviewer.  If not, see <http://www.gnu.org/licenses/>.

import functools
import os
import os.path
import multiprocessing
import Queue
import sys
import logging
import cPickle
import collections
import itertools

import numpy

import chunk
import nbt
import textures

"""
This module has routines related to generating all the chunks for a world
and for extracting information about available worlds

"""

base36decode = functools.partial(int, base=36)
cached = collections.defaultdict(dict)


def _convert_coords(chunks):
    """Takes the list of (chunkx, chunky, chunkfile) where chunkx and chunky
    are in the chunk coordinate system, and figures out the row and column in
    the image each one should be.

    returns mincol, maxcol, minrow, maxrow, chunks_translated
    chunks_translated is a list of (col, row, (chunkX, chunkY))

    The (chunkX, chunkY) tuple is the chunkCoords, used to identify the 
    chunk file
    """
    chunks_translated = []
    # columns are determined by the sum of the chunk coords, rows are the
    # difference
    item = chunks[0]
    mincol = maxcol = item[0] + item[1]
    minrow = maxrow = item[1] - item[0]
    for c in chunks:
        col = c[0] + c[1]
        mincol = min(mincol, col)
        maxcol = max(maxcol, col)
        row = c[1] - c[0]
        minrow = min(minrow, row)
        maxrow = max(maxrow, row)
        chunks_translated.append((col, row, (c[0],c[1])))

    return mincol, maxcol, minrow, maxrow, chunks_translated


def base36encode(number, alphabet='0123456789abcdefghijklmnopqrstuvwxyz'):
    '''
    Convert an integer to a base36 string.
    '''
    if not isinstance(number, (int, long)):
        raise TypeError('number must be an integer')
    
    newn = abs(number)
 
    # Special case for zero
    if number == 0:
        return '0'
 
    base36 = ''
    while newn != 0:
        newn, i = divmod(newn, len(alphabet))
        base36 = alphabet[i] + base36

    if number < 0:
        return "-" + base36
    return base36

class FakeAsyncResult:
    def __init__(self, string):
        self.string = string
    def get(self):
        return self.string

class WorldRenderer(object):
    """Renders a world's worth of chunks.
    worlddir is the path to the minecraft world
    cachedir is the path to a directory that should hold the resulting images.
    It may be the same as worlddir (which used to be the default).
    
    If chunklist is given, it is assumed to be an iterator over paths to chunk
    files to update. If it includes a trailing newline, it is stripped, so you
    can pass in file handles just fine.
    """
    def __init__(self, worlddir, cachedir, chunklist=None, lighting=False, night=False, spawn=False, useBiomeData=False):
        self.worlddir = worlddir
        self.caves = False
        self.lighting = lighting or night or spawn
        self.night = night or spawn
        self.spawn = spawn
        self.cachedir = cachedir
        self.useBiomeData = useBiomeData

        # figure out chunk format is in use
        # if mcregion, error out early until we can add support
        data = nbt.load(os.path.join(self.worlddir, "level.dat"))[1]['Data']
        #print data
        if not ('version' in data and data['version'] == 19132):
            logging.error("Sorry, This version of Minecraft-Overviewer only works with the new McRegion chunk format")
            sys.exit(1)

        if self.useBiomeData:
            textures.prepareBiomeData(worlddir)

        self.chunklist = chunklist

        # In order to avoid having to look up the cache file names in
        # ChunkRenderer, get them all and store them here
        # TODO change how caching works
        for root, dirnames, filenames in os.walk(cachedir):
            for filename in filenames:
                if not filename.endswith('.png') or not filename.startswith("img."):
                    continue
                dirname, dir_b = os.path.split(root)
                _, dir_a = os.path.split(dirname)
                _, x, z, cave, _ = filename.split('.', 4)
                dir = '/'.join((dir_a, dir_b))
                bits = '.'.join((x, z, cave))
                cached[dir][bits] = os.path.join(root, filename)

        #  stores Points Of Interest to be mapped with markers
        #  a list of dictionaries, see below for an example
        self.POI = []

        # if it exists, open overviewer.dat, and read in the data structure
        # info self.persistentData.  This dictionary can hold any information
        # that may be needed between runs.
        # Currently only holds into about POIs (more more details, see quadtree)
        self.pickleFile = os.path.join(self.cachedir,"overviewer.dat")
        if os.path.exists(self.pickleFile):
            with open(self.pickleFile,"rb") as p:
                self.persistentData = cPickle.load(p)
        else:
            # some defaults
            self.persistentData = dict(POI=[])



    def _get_chunk_renderset(self):
        """Returns a set of (col, row) chunks that should be rendered. Returns
        None if all chunks should be rendered"""
        if not self.chunklist:
            return None
        
        raise Exception("not yet working") ## TODO correctly reimplement this for mcregion
        # Get a list of the (chunks, chunky, filename) from the passed in list
        # of filenames
        chunklist = []
        for path in self.chunklist:
            if path.endswith("\n"):
                path = path[:-1]
            f = os.path.basename(path)
            if f and f.startswith("c.") and f.endswith(".dat"):
                p = f.split(".")
                chunklist.append((base36decode(p[1]), base36decode(p[2]),
                    path))

        if not chunklist:
            logging.error("No valid chunks specified in your chunklist!")
            logging.error("HINT: chunks are in your world directory and have names of the form 'c.*.*.dat'")
            sys.exit(1)

        # Translate to col, row coordinates
        _, _, _, _, chunklist = _convert_coords(chunklist)

        # Build a set from the col, row pairs
        inclusion_set = set()
        for col, row, filename in chunklist:
            inclusion_set.add((col, row))

        return inclusion_set
    
    def get_region_path(self, chunkX, chunkY):
        """Returns the path to the region that contains chunk (chunkX, chunkY)
        """
        
        chunkFile = "region/r.%s.%s.mcr" % (chunkX//32, chunkY//32)

        return os.path.join(self.worlddir, chunkFile)
    
    def findTrueSpawn(self):
        """Adds the true spawn location to self.POI.  The spawn Y coordinate
        is almost always the default of 64.  Find the first air block above
        that point for the true spawn location"""

        ## read spawn info from level.dat
        data = nbt.load(os.path.join(self.worlddir, "level.dat"))[1]
        spawnX = data['Data']['SpawnX']
        spawnY = data['Data']['SpawnY']
        spawnZ = data['Data']['SpawnZ']
   
        ## The chunk that holds the spawn location 
        chunkX = spawnX/16
        chunkY = spawnZ/16

        ## The filename of this chunk
        chunkFile = self.get_region_path(chunkX, chunkY)

        data=nbt.load_from_region(chunkFile, chunkX, chunkY)[1]
        level = data['Level']
        blockArray = numpy.frombuffer(level['Blocks'], dtype=numpy.uint8).reshape((16,16,128))

        ## The block for spawn *within* the chunk
        inChunkX = spawnX - (chunkX*16)
        inChunkZ = spawnZ - (chunkY*16)

        ## find the first air block
        while (blockArray[inChunkX, inChunkZ, spawnY] != 0):
            spawnY += 1
            if spawnY == 128:
                break

        self.POI.append( dict(x=spawnX, y=spawnY, z=spawnZ, 
                msg="Spawn", type="spawn", chunk=(inChunkX,inChunkZ)))

    def go(self, procs):
        """Starts the render. This returns when it is finished"""
        
        logging.info("Scanning chunks")
        raw_chunks = self._get_chunklist()
        logging.debug("Done scanning chunks")

        # Translate chunks to our diagonal coordinate system
        # TODO
        mincol, maxcol, minrow, maxrow, chunks = _convert_coords(raw_chunks)
        del raw_chunks # Free some memory

        self.chunkmap = self._render_chunks_async(chunks, procs)
        logging.debug("world chunkmap has len %d", len(self.chunkmap))


        self.mincol = mincol
        self.maxcol = maxcol
        self.minrow = minrow
        self.maxrow = maxrow

        self.findTrueSpawn()

    def _find_regionfiles(self):
        """Returns a list of all of the region files, along with their 
        coordinates

        Returns (regionx, regiony, filename)"""
        all_chunks = []

        for dirpath, dirnames, filenames in os.walk(os.path.join(self.worlddir, 'region')):
            if not dirnames and filenames and "DIM-1" not in dirpath:
                for f in filenames:
                    if f.startswith("r.") and f.endswith(".mcr"):
                        p = f.split(".")
                        all_chunks.append((int(p[1]), int(p[2]), 
                            os.path.join(dirpath, f)))
        return all_chunks

    def _get_chunklist(self):
        """Returns a list of all possible chunk coordinates, based on the 
        available regions files.  Note that not all chunk coordinates will
        exists.  The chunkrender will know how to ignore non-existant chunks

        returns a list of (chunkx, chunky, regionfile) where regionfile is
        the region file that contains this chunk

        TODO, a --cachedir implemetation should involved thie method

        """

        all_chunks = []

        regions = self._find_regionfiles()
        logging.debug("Found %d regions",len(regions))
        for region in regions:
            these_chunks = list(itertools.product(
                range(region[0]*32,region[0]*32 + 32),
                range(region[1]*32,region[1]*32 + 32)
                ))
            these_chunks = map(lambda x: (x[0], x[1], region[2]), these_chunks)
            assert(len(these_chunks) == 1024)
            all_chunks += these_chunks

        if not all_chunks:
            logging.error("Error: No chunks found!")
            sys.exit(1)

        logging.debug("Total possible chunks: %d", len(all_chunks))
        return all_chunks

    def _render_chunks_async(self, chunks, processes):
        """Starts up a process pool and renders all the chunks asynchronously.

        chunks is a list of (col, row, (chunkX, chunkY)).  Use chunkX,chunkY
        to find the chunk data in a region file

        Returns a dictionary mapping (col, row) to the file where that
        chunk is rendered as an image
        """
        # The set of chunks to render, or None for all of them. The logic is
        # slightly more compliated than it should seem, since we still need to
        # build the results dict out of all chunks, even if they're not being
        # rendered.
        inclusion_set = self._get_chunk_renderset()

        results = {}
        manager = multiprocessing.Manager()
        q = manager.Queue()

        if processes == 1:
            # Skip the multiprocessing stuff
            logging.debug("Rendering chunks synchronously since you requested 1 process")
            for i, (col, row, chunkXY) in enumerate(chunks):
                ##TODO##/if inclusion_set and (col, row) not in inclusion_set:
                ##TODO##/    # Skip rendering, just find where the existing image is
                ##TODO##/    _, imgpath = chunk.find_oldimage(chunkfile, cached, self.caves)
                ##TODO##/    if imgpath:
                ##TODO##/        results[(col, row)] = imgpath
                ##TODO##/        continue

                oldimg = chunk.find_oldimage(chunkXY, cached, self.caves)
                # TODO remove this shortcircuit
                if chunk.check_cache(self, chunkXY, oldimg):
                    result = oldimg[1]
                else:
                    #logging.debug("check cache failed, need to render (could be ghost chunk)")
                    result = chunk.render_and_save(chunkXY, self.cachedir, self, oldimg, queue=q)
                
                if result:
                    results[(col, row)] = result
                if i > 0:
                    try:
                        item = q.get(block=False)
                        if item[0] == "newpoi":
                            self.POI.append(item[1])
                        elif item[0] == "removePOI":
                            self.persistentData['POI'] = filter(lambda x: x['chunk'] != item[1], self.persistentData['POI'])
                    except Queue.Empty:
                        pass
                    if 1000 % i == 0 or i % 1000 == 0:
                        logging.info("{0}/{1} chunks rendered".format(i, len(chunks)))
        else:
            logging.debug("Rendering chunks in {0} processes".format(processes))
            pool = multiprocessing.Pool(processes=processes)
            asyncresults = []
            for col, row, chunkXY in chunks:
                ##TODO/if inclusion_set and (col, row) not in inclusion_set:
                ##TODO/    # Skip rendering, just find where the existing image is
                ##TODO/    _, imgpath = chunk.find_oldimage(chunkfile, cached, self.caves)
                ##TODO/    if imgpath:
                ##TODO/        results[(col, row)] = imgpath
                ##TODO/        continue

                oldimg = chunk.find_oldimage(chunkXY, cached, self.caves)
                if chunk.check_cache(self, chunkXY, oldimg):
                    result = FakeAsyncResult(oldimg[1])
                else:
                    result = pool.apply_async(chunk.render_and_save,
                            args=(chunkXY,self.cachedir,self, oldimg),
                            kwds=dict(cave=self.caves, queue=q))
                asyncresults.append((col, row, result))

            pool.close()

            for i, (col, row, result) in enumerate(asyncresults):
                results[(col, row)] = result.get()
                try:
                    item = q.get(block=False)
                    if item[0] == "newpoi":
                        self.POI.append(item[1])
                    elif item[0] == "removePOI":
                        self.persistentData['POI'] = filter(lambda x: x['chunk'] != item[1], self.persistentData['POI'])

                except Queue.Empty:
                    pass
                if i > 0:
                    if 1000 % i == 0 or i % 1000 == 0:
                        logging.info("{0}/{1} chunks rendered".format(i, len(asyncresults)))

            pool.join()
        logging.info("Done!")

        return results

def get_save_dir():
    """Returns the path to the local saves directory
      * On Windows, at %APPDATA%/.minecraft/saves/
      * On Darwin, at $HOME/Library/Application Support/minecraft/saves/
      * at $HOME/.minecraft/saves/

    """
    
    savepaths = []
    if "APPDATA" in os.environ:
        savepaths += [os.path.join(os.environ['APPDATA'], ".minecraft", "saves")]
    if "HOME" in os.environ:
        savepaths += [os.path.join(os.environ['HOME'], "Library",
                "Application Support", "minecraft", "saves")]
        savepaths += [os.path.join(os.environ['HOME'], ".minecraft", "saves")]

    for path in savepaths:
        if os.path.exists(path):
            return path

def get_worlds():
    "Returns {world # or name : level.dat information}"
    ret = {}
    save_dir = get_save_dir()

    # No dirs found - most likely not running from inside minecraft-dir
    if save_dir is None:
        return None

    for dir in os.listdir(save_dir):
        world_dat = os.path.join(save_dir, dir, "level.dat")
        if not os.path.exists(world_dat): continue
        info = nbt.load(world_dat)[1]
        info['Data']['path'] = os.path.join(save_dir, dir)
        if dir.startswith("World") and len(dir) == 6:
            try:
                world_n = int(dir[-1])
                ret[world_n] = info['Data']
            except ValueError:
                pass
        if 'LevelName' in info['Data'].keys():
            ret[info['Data']['LevelName']] = info['Data']

    return ret
