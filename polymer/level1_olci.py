#!/usr/bin/env python
# -*- coding: utf-8 -*-

from __future__ import print_function, division, absolute_import
import numpy as np
from polymer.block import Block
from polymer.common import L2FLAGS
from polymer.utils import raiseflag
from polymer.level1 import Level1_base
from netCDF4 import Dataset
from scipy.ndimage import map_coordinates
from datetime import datetime
from polymer.ancillary import Ancillary_NASA
import os
from collections import OrderedDict


class Level1_OLCI(Level1_base):
    '''
    OLCI reader using the netcdf module
    '''
    def __init__(self, dirname,
                 sline=0, eline=-1,
                 scol=0, ecol=-1,
                 blocksize=100, ancillary=None):

        self.sensor = 'OLCI'
        self.blocksize = blocksize

        if not os.path.isdir(dirname):
            dirname = os.path.dirname(dirname)

        self.dirname = dirname
        self.filename = dirname
        if ancillary is None:
            self.ancillary = Ancillary_NASA()
        else:
            self.ancillary = ancillary
        self.nc_datasets = {}

        # get product shape
        (totalheight, totalwidth) = self.get_ncroot('Oa01_radiance.nc').variables['Oa01_radiance'].shape
        print('height={}, width={}'.format(totalheight, totalwidth))

        self.init_shape(
                totalheight=totalheight,
                totalwidth=totalwidth,
                sline=sline,
                eline=eline,
                scol=scol,
                ecol=ecol)

        # file names
        self.band_names = {
                400 : 'Oa01_radiance', 412 : 'Oa02_radiance',
                443 : 'Oa03_radiance', 490 : 'Oa04_radiance',
                510 : 'Oa05_radiance', 560 : 'Oa06_radiance',
                620 : 'Oa07_radiance', 665 : 'Oa08_radiance',
                674 : 'Oa09_radiance', 681 : 'Oa10_radiance',
                709 : 'Oa11_radiance', 754 : 'Oa12_radiance',
                760 : 'Oa13_radiance', 764 : 'Oa14_radiance',
                767 : 'Oa15_radiance', 779 : 'Oa16_radiance',
                865 : 'Oa17_radiance', 885 : 'Oa18_radiance',
                900 : 'Oa19_radiance', 940 : 'Oa20_radiance',
                1020: 'Oa21_radiance',
                }

        self.band_index = {
                400 : 0, 412: 1, 443 : 2, 490: 3,
                510 : 4, 560: 5, 620 : 6, 665: 7,
                674 : 8, 681: 9, 709 :10, 754: 11,
                760 :12, 764: 13, 767 :14, 779: 15,
                865 :16, 885: 17, 900 :18, 940: 19,
                1020:20}

        self.F0 = self.get_ncroot('instrument_data.nc').variables['solar_flux'][:]
        self.lam0 = self.get_ncroot('instrument_data.nc').variables['lambda0'][:]

        # read quality flag meanings
        qf = self.get_ncroot('qualityFlags.nc').variables['quality_flags']
        fmask = qf.getncattr('flag_masks')
        fmeaning = str(qf.getncattr('flag_meanings')).split()
        self.quality_flags = {}
        for i in range(len(fmeaning)):
            self.quality_flags[fmeaning[i]] = fmask[i]

        # date initialization
        self.read_date()

        # ancillary data initialization
        self.init_ancillary()

    def read_date(self):
        var = self.get_ncroot('Oa01_radiance.nc')
        self.dstart = datetime.strptime(var.getncattr('start_time'), '%Y-%m-%dT%H:%M:%S.%fZ')
        self.dstop  = datetime.strptime(var.getncattr('stop_time'), '%Y-%m-%dT%H:%M:%S.%fZ')


    def init_ancillary(self):
        self.wind_speed = self.ancillary.get('wind_speed', self.date())

        self.ancillary_files = OrderedDict()
        self.ancillary_files.update(self.wind_speed.filename)

    def get_ncroot(self, filename):
        if filename in self.nc_datasets:
            return self.nc_datasets[filename]

        self.nc_datasets[filename] = Dataset(os.path.join(self.dirname, filename))

        return self.nc_datasets[filename]


    def read_band(self, band_name, size, offset):

        (ysize, xsize) = size
        (yoffset, xoffset) = offset

        # load netcdf object if not done already
        if band_name.startswith('Oa'):
            filename = band_name + '.nc'
            tiepoint = False
        elif band_name in ['latitude', 'longitude']:
            filename = 'geo_coordinates.nc'
            tiepoint = False
        elif band_name in ['SZA', 'SAA', 'OZA', 'OAA']:
            filename = 'tie_geometries.nc'
            tiepoint = True
        elif band_name in ['detector_index']:
            filename = 'instrument_data.nc'
            tiepoint = False
        elif band_name in ['total_ozone', 'sea_level_pressure']:
            filename = 'tie_meteo.nc'
            tiepoint = True
        elif band_name in ['quality_flags']:
            filename = 'qualityFlags.nc'
            tiepoint = False
        else:
            raise Exception('ERROR')


        root = self.get_ncroot(filename)
        var = root.variables[band_name]

        data = var[yoffset+self.sline:yoffset+self.sline+ysize, :]

        if tiepoint:
            shp = data.shape
            coords = np.meshgrid(np.linspace(0, shp[1]-1, self.totalwidth), np.arange(shp[0]))
            out = np.zeros((ysize, self.totalwidth), dtype='float32')
            map_coordinates(data, (coords[1], coords[0]), output=out)
            # FIXME: don't use 3rd order for azimuth angles
            data = out

        data = data[:, xoffset+self.scol:xoffset+self.scol+xsize]

        return data


    def date(self):
        return self.dstart + (self.dstop - self.dstart)//2


    def read_block(self, size, offset, bands):

        (ysize, xsize) = size
        nbands = len(bands)

        # initialize block
        block = Block(offset=offset, size=size, bands=bands)

        # read geometry
        block.latitude  = self.read_band('latitude',  size, offset)
        block.longitude = self.read_band('longitude', size, offset)

        # read geometry
        block.sza = self.read_band('SZA', size, offset)
        block.vza = self.read_band('OZA', size, offset)
        block.saa = self.read_band('SAA', size, offset)
        block.vaa = self.read_band('OAA', size, offset)

        # read LTOA
        block.Ltoa = np.zeros((ysize,xsize,nbands)) + np.NaN
        for iband, band in enumerate(bands):
            Ltoa_data = self.read_band(self.band_names[band], size, offset)
            block.Ltoa[:,:,iband] = Ltoa_data[:,:]

        # detector index
        di = self.read_band('detector_index', size, offset)

        # solar irradiance
        block.F0 = np.zeros((ysize, xsize, nbands), dtype='float32') + np.NaN
        for iband, band in enumerate(bands):
            block.F0[:,:,iband] = self.F0[self.band_index[band], di]

        # detector wavelength
        block.wavelen = np.zeros((ysize, xsize, nbands), dtype='float32') + np.NaN
        for iband, band in enumerate(bands):
            block.wavelen[:,:,iband] = self.lam0[self.band_index[band], di]

        # julian day and month
        block.jday = self.date().timetuple().tm_yday
        block.month = self.date().timetuple().tm_mon

        # read total ozone in kg/m2
        block.ozone = self.read_band('total_ozone', size, offset)
        block.ozone /= 2.1415e-5  # convert kg/m2 to DU

        # read sea level pressure in hPa
        block.surf_press = self.read_band('sea_level_pressure', size, offset)

        block.wind_speed = self.wind_speed[block.latitude, block.longitude]

        # quality flags
        bitmask = self.read_band('quality_flags', size, offset)
        block.bitmask = np.zeros(size, dtype='uint16')
        raiseflag(block.bitmask, L2FLAGS['LAND'],
                  bitmask & self.quality_flags['land'] != 0)
        raiseflag(block.bitmask, L2FLAGS['L1_INVALID'],
                  bitmask & self.quality_flags['invalid'] != 0)

        return block

    def blocks(self, bands_read):

        nblocks = int(np.ceil(float(self.height)/self.blocksize))
        for iblock in range(nblocks):

            # determine block size
            xsize = self.width
            if iblock == nblocks-1:
                ysize = self.height-(nblocks-1)*self.blocksize
            else:
                ysize = self.blocksize
            size = (ysize, xsize)

            # determine the block offset
            xoffset = 0
            yoffset = iblock*self.blocksize
            offset = (yoffset, xoffset)

            yield self.read_block(size, offset, bands_read)

    def attributes(self, datefmt):
        attr = OrderedDict()
        attr['l1_filename'] = self.filename
        attr['start_time'] = self.dstart.strftime(datefmt)
        attr['stop_time'] = self.dstop.strftime(datefmt)

        attr.update(self.ancillary_files)

        return attr

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

