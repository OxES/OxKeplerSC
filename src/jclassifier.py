from __future__ import division
from math import copysign, log
from .core import *
from .mugp import MuGP
from pyde.de import DiffEvol
from .fmodels import models
from scipy.optimize import minimize

fm_transit = models.m_transit
fm_jump = models.m_jump

def bic(nln, npv, npt):
    return 2*nln + npv*log(npt)

class JumpClassifier(object):
    classes = 'noise slope jump transit flare'.split()
    npar    = [0, 0, 4, 4, 4]
    
    def __init__(self, kdata, hp, window_width=75, kernel='e'):
        self.gp = MuGP(kernel=kernel)
        self._kdata = kdata
        self.cadence = self._kdata.cadence
        self.flux = self._kdata.normalized_flux
        self.hp = hp
        self._ww = window_width
        self._hw = self._ww//2
        self.gp.set_parameters(self.hp)


    def classify(self, jumps, de_niter=200, de_npop=30):
        """Classify flux discontinuities

        Classifies given flux discontinuities (jumps) as noise, jump, transit, or flare. 

        parameters
        ----------
        jumps    : Jump or a list of Jumps

        de_niter : int, optional
                   number of differential evolution iterations

        de_npop  : int, optional,
                   size of the differential evolution population

        Notes
        -----
        The classification is based on a simple BIC comparison. That is, we fit several
        models to the discontinuities and select the one with the lowest BIC value.

        The ln likelihood space can be pretty nasty even when our models are simple
        (thanks to GPs), so we do a small global optimisation run using Differential
        Evolution (DE) before a local optimisation. The DE run is the most time consuming
        part of the process, but necessary for realiable classification.
        """
        if isinstance(jumps, list):
            [self._classify_single(j, de_niter, de_npop) for j in jumps]
        elif isinstance(jumps, Jump):
            self._classify_single(jumps, de_niter, de_npop)
        else:
            raise NotImplementedError('jumps must be a list of jumps or a single jump.')
        
            
    def _classify_single(self, jump, de_niter=150, de_npop=30):
        idx = np.argmin(np.abs(self.cadence-jump.pos))
        self._sl = sl   = np.s_[max(0, idx-self._hw) : min(idx+self._hw, self.cadence.size)]
        self._cd = cad  = self.cadence[sl].copy()
        self._fl = flux = self.flux[sl].copy()  + 1.
        flux[:] = flux / median(flux) - 1.
        self.gp.compute(cad)

        jamp, jpos = abs(jump.amp), jump.pos

        ## Calculate the maximum log likelihoods
        ## -------------------------------------

        ## Noise
        ## -----
        nlns = self.nlnlike_noise([], cad, flux)

        pvsl = np.polyfit(cad, flux, 2)
        nlsl = self.nlnlike_slope(pvsl, cad, flux)

        ## Jump
        ## ----
        pvjm, nljm = self.fit_jump(jump, cad, flux, de_npop, de_niter)

        ## Transit
        ## -------
        pvtr, nltr = self.fit_transit(jump, cad, flux, de_npop, de_niter)

        ## Flare
        ## -----
        rflare= minimize(self.nlnlike_flare, [abs(jump.amp), jump.pos, 0.5], 
                         (cad, flux), method = 'Nelder-Mead')
        pvfl, nlfl = rflare.x, rflare.fun

        pvs  = [[], pvsl, pvjm, pvtr, pvfl]
        nlns = [nlns, nlsl, nljm, nltr, nlfl]
        bics = [bic(nln, npv, cad.size) for nln,npv in zip(nlns, self.npar)]
        
        cid = np.argmin(bics)
        jump.type = self.classes[cid]
        jump.bics = bics
        jump._pv = pvs[cid]

            
    def fit_jump(self, jump, cadence, flux, de_npop=30, de_niter=100):
        jamp, jpos = abs(jump.amp), jump.pos
        de = DiffEvol(lambda pv: self.nlnlike_jump(pv, cadence, flux),
                        [[    jpos-2,     jpos+2],
                        [         1,          3],
                        [ 0.75*jamp,  1.25*jamp],
                        [ -0.2*flux.std(), 0.2*flux.std()]],
                        npop=de_npop)
        de.optimize(de_niter)

        rjump = minimize(self.nlnlike_jump, de.minimum_location, 
                         (cadence, flux), method = 'Nelder-Mead')
        return rjump.x, rjump.fun

    
    def fit_transit(self, jump, cadence, flux, de_npop=30, de_niter=100):
        jamp, jpos = abs(jump.amp), jump.pos
        de = DiffEvol(lambda pv: self.nlnlike_transit(pv, cadence, flux),
                      [[0.8*jamp, 1.2*jamp],
                       [  jpos-5,   jpos+5],
                       [     1.2,      50.],
                       [ -0.2*flux.std(), 0.2*flux.std()]],
                      npop=de_npop)
        de.optimize(de_niter)

        rtransit = minimize(self.nlnlike_transit, de.minimum_location, 
                         (cadence, flux), method = 'Nelder-Mead')
        return rtransit.x, rtransit.fun


        
    def nlnlike_noise(self, pv, cadence, flux):
        return -self.gp.lnlikelihood(cadence, flux, freeze_k=True)

    def nlnlike_slope(self, pv, cadence, flux):
        return -self.gp.lnlikelihood(cadence, flux-self.m_slope(pv, cadence), freeze_k=True)
    
    def nlnlike_jump(self, pv, cadence, flux):
        if np.any(pv[:2] < 0) or not (0.5 < pv[1] < 3.0):
            return inf
        return -self.gp.lnlikelihood(cadence, flux-self.m_jump(pv, cadence), freeze_k=True)

    def nlnlike_transit(self, pv, cadence, flux):
        if np.any(pv[:-1] <= 0.) or not (self._cd[0] < pv[1] < self._cd[-1]) or not (1. < pv[2] < 50.):
            return inf
        return -self.gp.lnlikelihood(cadence, flux-self.m_transit(pv, cadence), freeze_k=True)

    def nlnlike_flare(self, pv, cadence, flux):
        if np.any(pv <= 0.) or not (self._cd[0] < pv[1] < self._cd[-1]) or (pv[2] > 10):
            return inf
        return -self.gp.lnlikelihood(cadence, flux-self.m_flare(pv, cadence), freeze_k=True)


    def m_slope(self, pv, cadence):
        """
        0 : slope
        1 : intercept
        """
        return np.poly1d(pv)(cadence)


    def m_jump(self, pv, cadence):
        """
        0 : jump cadence
        1 : jump width
        2 : jump amplitude
        """
        return fm_jump(*pv, cadence=cadence)

    
    def m_transit(self, pv, cadence):
        """
        0 : transit depth
        1 : transit center
        2 : transit duration
        """
        return fm_transit(*pv, cadence=cadence)


    def m_transit_python(self, pv, cadence):
        """
        0 : transit depth
        1 : transit center
        2 : transit duration
        """
        model = np.zeros(cadence.size, np.float64)
        if np.any(pv < 0):
            return None
        
        hdur = 0.5*pv[2]
        cstart = int(np.floor(pv[1] - hdur))
        cend   = int(np.floor(pv[1] + hdur))
    
        for i,cad in enumerate(cadence):
            if (cad > cstart) & (cad < cend):
                model[i] = 1
            elif (cad == cstart):
                model[i] = 1 - (pv[1] - hdur - cstart)
            elif (cad == cend):
                model[i] = pv[1] + hdur - cend
        return -pv[0] * model
    

    def m_flare(self, pv, cadence):
        """
        0 : flare amplitude
        1 : start position
        2 : decay length
        """
        if np.any(pv < 0):
            return None

        model = np.zeros(cadence.size, np.float64)
        cmask = cadence >= pv[1]
        model[cmask] = pv[0]*np.exp(-(cadence[cmask]-pv[1])/pv[2])
        return model

