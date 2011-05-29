#!/usr/bin/python
#
# flam3cuda, one of a surprisingly large number of ports of the fractal flame
# algorithm to NVIDIA GPUs.
#
# This one is copyright 2010 Steven Robertson <steven@strobe.cc>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 or later
# as published by the Free Software Foundation.

import os
import sys

from pprint import pprint
from ctypes import *

import numpy as np
np.set_printoptions(precision=5, edgeitems=20)
import scipy

import pyglet
import pycuda.autoinit

from fr0stlib.pyflam3 import *
from fr0stlib.pyflam3._flam3 import *

import cuburn._pyflam3_hacks
from cuburn.render import *
from cuburn.code.mwc import MWCTest
from cuburn.code.iter import render, membench

# Required on my system; CUDA doesn't yet work with GCC 4.5
os.environ['PATH'] = ('/usr/x86_64-pc-linux-gnu/gcc-bin/4.4.5:'
                     + os.environ['PATH'])

def main(args):
    if '-t' in args:
        MWCTest.test_mwc()
        membench()


    with open(args[1]) as fp:
        genomes = Genome.from_string(fp.read())
    anim = Animation(genomes)
    accum, den = render(anim.features, genomes)

    noalpha = np.delete(accum, 3, axis=2)
    scipy.misc.imsave('rendered.png', noalpha)
    scipy.misc.imsave('rendered.jpg', noalpha)

    if '-g' not in args:
        return

    window = pyglet.window.Window(anim.features.width, anim.features.height)
    imgbuf = (np.minimum(accum * 255, 255)).astype(np.uint8)
    image = pyglet.image.ImageData(anim.features.width, anim.features.height,
                                   'RGBA', imgbuf.tostring(),
                                   -anim.features.width * 4)
    tex = image.texture

    #pal = (anim.ctx.ptx.instances[PaletteLookup].pal * 255.).astype(np.uint8)
    #image2 = pyglet.image.ImageData(256, 16, 'RGBA', pal.tostring())

    @window.event
    def on_draw():
        window.clear()
        tex.blit(0, 0)
        #image2.blit(0, 0)

    @window.event
    def on_key_press(sym, mod):
        if sym == pyglet.window.key.Q:
            pyglet.app.exit()

    pyglet.app.run()

if __name__ == "__main__":
    if len(sys.argv) < 2 or not os.path.isfile(sys.argv[1]):
        print "Last argument must be a path to a genome file"
        sys.exit(1)
    main(sys.argv)

