#!/usr/bin/env python
# encoding: utf-8


import numpy as np
from luts import read_mlut_hdf, Idx
import warnings
from utils import stdNxN
from common import BITMASK_INVALID, L2FLAGS
from luts import LUT
from pyhdf.SD import SD

# cython imports
# import pyximport ; pyximport.install()
from polymer_main import PolymerMinimizer
from water import ParkRuddick




def coeff_sun_earth_distance(jday):
    jday -= 1   # TODO: verify (0-based is consistent with C-version)

    A=1.00014
    B=0.01671
    C=0.9856002831
    D=3.4532858
    E=360.
    F=0.00014

    coef  = 1./((A - B*np.cos(2*np.pi*(C*jday - D)/E) - F*np.cos(4*np.pi*(C*jday - D)/E))**2)

    return coef


class InitCorr(object):
    '''
    Implementation of the initial corrections
    (convert to reflectance, gaseous correction, cloud mask,
    Rayleigh correction)
    '''
    def __init__(self, params):
        self.params = params

        # read the look-up table
        self.mlut = read_mlut_hdf(params.lut_file)


    def init_minimizer(self):
        '''
        Initialization of the minimizer class
        '''
        watermodel = ParkRuddick('/home/francois/MERIS/POLYMER/auxdata/common/')

        return PolymerMinimizer(watermodel, self.params)


    def convert_reflectance(self, block):

        block.Rtoa = np.zeros(block.Ltoa.shape)+np.NaN

        coef = coeff_sun_earth_distance(block.jday)

        ok = (block.bitmask & BITMASK_INVALID) == 0

        for i in xrange(block.nbands):

            block.Rtoa[ok,i] = block.Ltoa[ok,i]*np.pi/(block.mus[ok]*block.F0[ok,i]*coef)


    def read_no2_data(self, month):

        # read total and tropospheric no2 data
        hdf = SD(self.params.no2_climatology)
        self.no2_total_data = hdf.select('tot_no2_{:02d}'.format(month)).get()

        self.no2_tropo_data = hdf.select('trop_no2_{:02d}'.format(month)).get()
        hdf.end()

        # read fraction of tropospheric NO2 above 200mn
        hdf = SD(self.params.no2_frac200m)
        self.no2_frac200m_data = hdf.select('f_no2_200m').get()
        hdf.end()


    def get_no2(self, block):
        '''
        returns no2_frac, no2_tropo, no2_strat
        '''

        # get month
        assert not isinstance(block.jday, np.ndarray)
        month = int((float(block.jday)/30.5)) + 1
        if month > 12:
            month = 12

        try:
            self.no2_tropo_data
        except:
            self.read_no2_data(month)

        # coordinates of current block in 1440x720 grid
        assert self.no2_tropo_data.shape == (720, 1440)
        ilat = (4*(90 - block.latitude)).astype('int')
        ilon = (4*block.longitude).astype('int')
        ilon[ilon<0] += 4*360

        no2_tropo = self.no2_tropo_data[ilat,ilon]*1e15
        no2_strat = (self.no2_total_data[ilat,ilon]
                     - self.no2_tropo_data[ilat,ilon])*1e15

        # coordinates of current block in 360x180 grid
        ilat = (0.5*(90 - block.latitude)).astype('int')
        ilon = (0.5*(block.longitude)).astype('int')
        ilon[ilon<0] += 360/2
        no2_frac = self.no2_frac200m_data[ilat,ilon]

        return no2_frac, no2_tropo, no2_strat


    def gas_correction(self, block):

        params = self.params

        block.Rtoa_gc = np.zeros(block.Rtoa.shape, dtype='float32') + np.NaN

        ok = (block.bitmask & BITMASK_INVALID) == 0

        #
        # ozone correction
        #
        # make sure that ozone is in DU
        if (block.ozone < 50).any() or (block.ozone > 1000).any():
            raise Exception('Error, ozone is assumed in DU')

        # bands loop
        for i, b in enumerate(block.bands):

            tauO3 = params.K_OZ[b] * block.ozone[ok] * 1e-3  # convert from DU to cm*atm

            # ozone transmittance
            trans_O3 = np.exp(-tauO3 * block.air_mass[ok])

            block.Rtoa_gc[ok,i] = block.Rtoa[ok,i]/trans_O3

        #
        # NO2 correction
        #
        no2_frac, no2_tropo, no2_strat = self.get_no2(block)

        no2_tr200 = no2_frac * no2_tropo

        for i, b in enumerate(block.bands):

            k_no2 = params.K_NO2[b]

            a_285 = k_no2 * (1.0 - 0.003*(285.0-294.0))
            a_225 = k_no2 * (1.0 - 0.003*(225.0-294.0))

            tau_to200 = a_285*no2_tr200 + a_225*no2_strat

            t_no2  = np.exp(-(tau_to200[ok]/block.mus[ok]))
            t_no2 *= np.exp(-(tau_to200[ok]/block.muv[ok]))

            block.Rtoa_gc[ok,i] /= t_no2

    def cloudmask(self, block):
        '''
        Polymer basic cloud mask
        '''
        params = self.params
        ok = (block.bitmask & BITMASK_INVALID) == 0

        inir_block = block.bands.index(params.band_cloudmask)
        inir_lut = params.bands_lut.index(params.band_cloudmask)
        block.Rnir = block.Rtoa_gc[:,:,inir_block] - self.mlut['Rmol'][
                Idx(block.muv),
                Idx(block.raa),
                Idx(block.mus),
                inir_lut]
        cloudmask = block.Rnir > params.thres_Rcloud
        cloudmask |= stdNxN(block.Rnir, 3, ok) > params.thres_Rcloud_std

        block.bitmask += L2FLAGS['CLOUD_BASE'] * cloudmask.astype('uint8')


    def rayleigh_correction(self, block):
        '''
        Rayleigh correction
        + transmission interpolation
        '''
        params = self.params
        mlut = self.mlut
        if params.partial >= 2:
            return

        block.Rprime = np.zeros(block.Ltoa.shape, dtype='float32')+np.NaN
        block.Rmol = np.zeros(block.Ltoa.shape, dtype='float32')+np.NaN
        block.Tmol = np.zeros(block.Ltoa.shape, dtype='float32')+np.NaN

        ok = (block.bitmask & BITMASK_INVALID) == 0

        for i in xrange(block.nbands):
            ilut = params.bands_lut.index(block.bands[i])

            Rmolgli = mlut['Rmolgli'][
                    Idx(block.muv[ok]),
                    Idx(block.raa[ok]),
                    Idx(block.mus[ok]),
                    ilut, Idx(block.wind_speed[ok])]

            wl = block.wavelen[ok,i]
            wl0 = self.params.central_wavelength[block.bands[i]]

            # wavelength adjustment
            Rmolgli *= (wl/wl0)**(-4.)

            # adjustment for atmospheric pressure
            Rmolgli *= block.surf_press[ok]/1013.

            block.Rmol[ok,i] = Rmolgli

            block.Rprime[ok,i] = block.Rtoa_gc[ok,i] - Rmolgli

            # TODO: share axes indices
            # and across wavelengths
            block.Tmol[ok,i]  = mlut['Tmolgli'][Idx(block.mus[ok]),
                    ilut, Idx(block.wind_speed[ok])]
            block.Tmol[ok,i] *= mlut['Tmolgli'][Idx(block.muv[ok]),
                    ilut, Idx(block.wind_speed[ok])]

            # correction for atmospheric pressure
            taumol = 0.00877*((block.wavelen[ok,i]/1000.)**-4.05)
            block.Tmol[ok,i] *= np.exp(-taumol/2. * (block.surf_press[ok]/1013. - 1.) * block.air_mass[ok])


def polymer(level1, params, level2):
    '''
    Polymer atmospheric correction
    '''

    # initialize output file
    level2.init(level1)

    c = InitCorr(params)

    opt = c.init_minimizer()

    # loop over the blocks
    for block in level1.blocks(params.bands_read()):

        c.convert_reflectance(block)

        c.gas_correction(block)

        c.cloudmask(block)

        c.rayleigh_correction(block)

        opt.minimize(block, params)

        level2.write(block)

    level2.finish(params)

    return level2

