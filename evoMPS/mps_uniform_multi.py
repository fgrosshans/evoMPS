# -*- coding: utf-8 -*-
"""
Created on Thu Oct 13 17:29:27 2011

@author: Ashley Milsted

"""
import numpy as np
import scipy as sp
import scipy.linalg as la
import scipy.sparse.linalg as las
import tdvp_common as tm
import matmul as m
import math as ma
import logging

log = logging.getLogger(__name__)

class EOp:
    def __init__(self, As1, As2, left):
        """Creates a new LinearOperator interface to the superoperator E.
        
        This is a wrapper to be used with SciPy's sparse linear algebra routines.
        
        Parameters
        ----------
        A1 : ndarray
            Ket parameter tensor. 
        A2 : ndarray
            Bra parameter tensor.
        left : bool
            Whether to multiply with a vector to the left (or to the right).
        """
        self.As1 = As1
        self.As2 = As2
        
        self.D1 = As1[0].shape[1]
        self.D2 = As2[0].shape[1]
        
        self.shape = (self.D1 * self.D2, self.D1 * self.D2)
        
        self.dtype = np.dtype(As1[0].dtype)
        
        self.calls = 0
        
        self.left = left
        
        if left:
            self.eps = tm.eps_l_noop
        else:
            self.eps = tm.eps_r_noop
    
    def matvec(self, v):
        """Matrix-vector multiplication. 
        Result = Ev or vE (if self.left == True).
        """
        x = v.reshape((self.D1, self.D2))
        
        if self.left:           
            for n in xrange(len(self.As1)):
                x = self.eps(x, self.As1[n], self.As2[n])
        else:
            for n in xrange(len(self.As1) - 1, -1, -1):
                x = self.eps(x, self.As1[n], self.As2[n])
            
        Ex = x
        
        self.calls += 1
        
        return Ex.ravel()

class EvoMPS_MPS_Uniform(object):   
        
    def __init__(self, D, q, L, dtype=None):
        """Creates a new EvoMPS_MPS_Uniform object.
        
        This class implements basic operations on a uniform 
        (translation-invariant) MPS in the thermodynamic limit.
        
        self.A is the parameter tensor and has shape (q, D, D).
        
        Parameters
        ----------
        D : int
            The bond-dimension.
        q : int
            The single-site Hilbert-space dimension.
        L : int
            Block length.
        dtype : numpy-compatible dtype
            The data-type to be used. The default is double-precision complex.
        """
        self.odr = 'C' 
        
        if dtype is None:
            self.typ = np.complex128
        
        self.itr_rtol = 1E-13
        self.itr_atol = 1E-14
        
        self.zero_tol = sp.finfo(self.typ).resolution
        """Tolerance for detecting zeros. This is used when (pseudo-) inverting 
           l and r."""
        
        self.pow_itr_max = 2000
        """Maximum number of iterations to use in the power-iteration algorithm 
           for finding l and r."""
           
        self.ev_use_arpack = True
        """Whether to use ARPACK (implicitly restarted Arnoldi iteration) to 
           find l and r."""
           
        self.ev_arpack_nev = 1
        """The number of eigenvalues to find when calculating l and r. If the
           spectrum is approximately degenerate, this may need to be increased."""
           
        self.ev_arpack_ncv = None
        """The number of intermediate vectors stored during arnoldi iteration.
           See the documentation for scipy.sparse.linalg.eig()."""
                
        self.symm_gauge = True
        """Whether to use symmetric canonical form or (if False) right canonical form."""
        
        self.sanity_checks = False
        """Whether to perform additional (potentially costly) sanity checks."""

        self.check_fac = 50
        self.eps = np.finfo(self.typ).eps
        
        self.userdata = None        
        """This variable is saved (pickled) with the state. 
           For storing arbitrary user data."""
        
        #Initialize some more instance attributes.
        self.itr_l = 0
        """Contains the number of eigenvalue solver iterations needed to find l."""
        self.itr_r = 0
        """Contains the number of eigenvalue solver iterations needed to find r."""
        
        self.S_hcs = sp.empty((L), dtype=sp.complex128)
        self.S_hcs.fill(sp.NaN)
        """After calling restore_CF() or update(restore_CF=True), this contains
           the von Neumann entropy of one infinite half of the system."""
                
        self._init_arrays(D, q, L)
                    
        self.randomize()

    def randomize(self, do_update=True):
        """Randomizes the parameter tensors self.A.
        
        Parameters
        ----------
        do_update : bool (True)
            Whether to perform self.update() after randomizing.
        """
        for A in self.As:
            m.randomize_cmplx(A)
            A /= la.norm(A)
        
        if do_update:
            self.update()
            
    def add_noise(self, fac=1.0, do_update=True):
        """Adds some random (white) noise of a given magnitude to the parameter 
        tensors A.
        
        Parameters
        ----------
        fac : number
            A factor determining the amplitude of the random noise.
        do_update : bool (True)
            Whether to perform self.update() after randomizing.
        """
        for A in self.As:
            norm = la.norm(A)
            f = fac * (norm / (self.q * self.D**2))
            R = np.empty_like(A)
            m.randomize_cmplx(R, -f / 2.0, f / 2.0)
            
            A += R
        
        if do_update:
            self.update()
    
    def _init_arrays(self, D, q, L):
        self.D = D
        self.q = q
        self.L = L
        
        self.As = []
        self.AAs = []
        self.ls = []
        self.rs = []
        for m in xrange(L):
            self.As.append(np.zeros((q, D, D), dtype=self.typ, order=self.odr))
            self.AAs.append(np.zeros((q, q, D, D), dtype=self.typ, order=self.odr))
            self.ls.append(np.ones_like(self.As[0][0]))
            self.rs.append(np.ones_like(self.As[0][0]))
            
        self.lL_before_CF = self.ls[-1]
        self.rL_before_CF = self.rs[-1]
            
        self.conv_l = True
        self.conv_r = True
        
        self.tmp = np.zeros_like(self.As[0][0])
    
    def _calc_lr_ARPACK(self, x, tmp, calc_l=False, A1s=None, A2s=None, rescale=True,
                        tol=1E-14, ncv=None, k=1):
        if A1s is None:
            A1s = self.As
        if A2s is None:
            A2s = self.As
            
        if self.D == 1:
            x.fill(1)
            if calc_l:
                for k in xrange(len(A1s)):
                    x = tm.eps_l_noop(x, A1s[k], A2s[k])
            else:
                for k in xrange(len(A1s) - 1, -1, -1):
                    x = tm.eps_r_noop(x, A1s[k], A2s[k])
                
            ev = x[0, 0]
            
            if rescale and not abs(ev - 1) < tol:
                A1s[0] *= 1 / sp.sqrt(ev)
            
            return x, True, 1
                        
        try:
            norm = la.get_blas_funcs("nrm2", [x])
        except (ValueError, AttributeError):
            norm = np.linalg.norm
    
        n = x.size #we will scale x so that stuff doesn't get too small
        
        #start = time.clock()
        opE = EOp(A1s, A2s, calc_l)
        x *= n / norm(x.ravel())
        try:
            ev, eV = las.eigs(opE, which='LM', k=k, v0=x.ravel(), tol=tol, ncv=ncv)
            conv = True
        except las.ArpackNoConvergence:
            log.warning("Reset! (l? %s)", calc_l)
            ev, eV = las.eigs(opE, which='LM', k=k, tol=tol, ncv=ncv)
            conv = True
            
        #print ev2
        #print ev2 * ev2.conj()
        ind = ev.argmax()
        ev = np.real_if_close(ev[ind])
        ev = np.asscalar(ev)
        eV = eV[:, ind]
        
        #remove any additional phase factor
        eVmean = eV.mean()
        eV *= sp.sqrt(sp.conj(eVmean) / eVmean)
        
        if eV.mean() < 0:
            eV *= -1

        eV = eV.reshape(self.D, self.D)
        
        eV *= n / norm(eV.ravel())
        
        x[:] = eV
        
        #print "splinalg: %g" % (time.clock() - start)   
        
        #print "Herm? %g" % norm((eV - m.H(eV)).ravel())
        #print "Norm of diff: %g" % norm((eV - x).ravel())
        #print "Norms: (%g, %g)" % (norm(eV.ravel()), norm(x.ravel()))
                    
        if rescale and not abs(ev - 1) < tol:
            A1s[0] *= 1 / sp.sqrt(ev)
            if self.sanity_checks:
                if not A1s[0] is A2s[0]:
                    log.warning("Sanity check failed: Re-scaling with A1 <> A2!")
                tmp = opE.matvec(x.ravel())
                ev = tmp.mean() / x.mean()
                if not abs(ev - 1) < tol:
                    log.warning("Sanity check failed: Largest ev after re-scale = %s", ev)
        
        return x, conv, opE.calls
        
    def _calc_E_largest_eigenvalues(self, tol=1E-6, k=2, ncv=10):
        opE = EOp(self.As, self.As, False)
        
        r = np.asarray(self.rs[0])
        
        ev = las.eigs(opE, which='LM', k=k, v0=r.ravel(), tol=tol, ncv=ncv,
                      return_eigenvectors=False)
                          
        return ev
        
    def calc_E_gap(self, tol=1E-6, k=2, ncv=None):
        """
        Calculates the spectral gap of E by calculating the second-largest eigenvalue.
        
        The result is the difference between the largest eigenvalue and the 
        magnitude of the second-largest divided by the largest 
        (which should be equal to 1).
        
        This is related to the correlation length. See self.correlation_length().
        
        Parameters
        ----------
        tol : float
            Tolerance for second-largest eigenvalue.
        ncv : int
            Number of Arnoldii basis vectors to store.
        """
        ev = self._calc_E_largest_eigenvalues(tol=tol, k=k, ncv=ncv)
                          
        ev1_mag = abs(ev).max()
        ev2_mag = abs(ev).min()
        
        return ((ev1_mag - ev2_mag) / ev1_mag)
        
    def correlation_length(self, tol=1E-12, k=2, ncv=None):
        """
        Calculates the correlation length in units of the lattice spacing.
        
        The correlation length is equal to the inverse of the natural logarithm
        of the maginitude of the second-largest eigenvalue of the transfer 
        (or super) operator E.
        
        Parameters
        ----------
        tol : float
            Tolerance for second-largest eigenvalue.
        ncv : int
            Number of Arnoldii basis vectors to store.
        """
        ev = self._calc_E_largest_eigenvalues(tol=tol, k=k, ncv=ncv)
        log.debug("Eigenvalues of the transfer operator: %s", ev)
        
        #We only require the absolute values, and sort() does not handle
        #complex values nicely (it sorts by real part).
        ev = abs(ev)
        
        ev.sort()
        log.debug("Eigenvalue magnitudes of the transfer operator: %s", ev)
                          
        ev1 = ev[-1]
        
        if abs(ev1 - 1) > tol:
            log.warning("Warning: Largest eigenvalue != 1")

        while True:
            if ev.shape[0] > 1 and (ev1 - ev[-1]) < tol:
                ev = ev[:-1]
            else:
                break

        if ev.shape[0] == 0:
            log.warning("Warning: No eigenvalues detected with magnitude significantly different to largest.")
            return sp.NaN
        
        return -1 / sp.log(ev[-1])
                
    
    def calc_lr(self):
        """Determines the dominant left and right eigenvectors of the transfer 
        operator E.
        
        Uses an iterative method (e.g. Arnoldi iteration) to determine the
        largest eigenvalue and the correspoinding left and right eigenvectors,
        which are stored as self.l and self.r respectively.
        
        The parameter tensor self.A is rescaled so that the largest eigenvalue
        is equal to 1 (thus normalizing the state).
        
        The largest eigenvalue is assumed to be non-degenerate.
        
        """
        tmp = np.empty_like(self.tmp)
        
        #Make sure...
        self.lL_before_CF = np.asarray(self.lL_before_CF)
        self.rL_before_CF = np.asarray(self.rL_before_CF)
        
        self.ls[-1], self.conv_l, self.itr_l = self._calc_lr_ARPACK(self.lL_before_CF, tmp,
                                               calc_l=True,
                                               tol=self.itr_rtol,
                                               k=self.ev_arpack_nev,
                                               ncv=self.ev_arpack_ncv)
                                        
        self.lL_before_CF = self.ls[-1].copy()

        self.rs[-1], self.conv_r, self.itr_r = self._calc_lr_ARPACK(self.rL_before_CF, tmp, 
                                               calc_l=False,
                                               tol=self.itr_rtol,
                                               k=self.ev_arpack_nev,
                                               ncv=self.ev_arpack_ncv)
        self.rL_before_CF = self.rs[-1].copy()
            
        #normalize eigenvectors:

        if self.symm_gauge:
            norm = m.adot(self.ls[-1], self.rs[-1]).real
            itr = 0 
            while not np.allclose(norm, 1, atol=1E-13, rtol=0) and itr < 10:
                self.ls[-1] *= 1. / ma.sqrt(norm)
                self.rs[-1] *= 1. / ma.sqrt(norm)
                
                norm = m.adot(self.ls[-1], self.rs[-1]).real
                
                itr += 1
                
            if itr == 10:
                log.warning("Warning: Max. iterations reached during normalization!")
        else:
            fac = self.D / np.trace(self.rs[-1]).real
            self.ls[-1] *= 1 / fac
            self.rs[-1] *= fac

            norm = m.adot(self.ls[-1], self.rs[-1]).real
            itr = 0 
            while not np.allclose(norm, 1, atol=1E-13, rtol=0) and itr < 10:
                self.ls[-1] *= 1. / norm
                norm = m.adot(self.ls[-1], self.rs[-1]).real
                itr += 1
                
            if itr == 10:
                log.warning("Warning: Max. iterations reached during normalization!")

        for k in xrange(len(self.As) - 1, 0, -1):
            self.rs[k - 1] = tm.eps_r_noop(self.rs[k], self.As[k], self.As[k])
            
        for k in xrange(0, len(self.As) - 1):
            self.ls[k] = tm.eps_l_noop(self.ls[k - 1], self.As[k], self.As[k])

        if self.sanity_checks:
            for k in xrange(self.L):
                l = self.ls[k]
                for j in sp.arange(k + 1, k + self.L + 1) % self.L:
                    l = tm.eps_l_noop(l, self.As[j], self.As[j])
                if not np.allclose(l, self.ls[k],
                rtol=self.itr_rtol*self.check_fac, 
                atol=self.itr_atol*self.check_fac):
                    log.warning("Sanity check failed: l%u bad! Off by: %s", k,
                                la.norm(tm.eps_l_noop(self.l, self.A, self.A) - self.l))

                r = self.rs[k]
                for j in sp.arange(k, k - self.L, -1) % self.L:
                    r = tm.eps_r_noop(r, self.As[j], self.As[j])
                if not np.allclose(r, self.rs[k],
                rtol=self.itr_rtol*self.check_fac,
                atol=self.itr_atol*self.check_fac):
                    log.warning("Sanity check failed: r%u bad! Off by: %s", k,
                                la.norm(tm.eps_r_noop(self.r, self.A, self.A) - self.r))
                
                if not np.allclose(self.ls[k], m.H(self.ls[k]),
                rtol=self.itr_rtol*self.check_fac, 
                atol=self.itr_atol*self.check_fac):
                    log.warning("Sanity check failed: l%u is not hermitian! Off by: %s",
                                k, la.norm(self.ls[-k] - m.H(self.ls[k])))
    
                if not np.allclose(self.rs[k], m.H(self.rs[k]),
                rtol=self.itr_rtol*self.check_fac, 
                atol=self.itr_atol*self.check_fac):
                    log.warning("Sanity check failed: r%u is not hermitian! Off by: %s",
                                k, la.norm(self.rs[k] - m.H(self.rs[k])))
                
                minev = la.eigvalsh(self.ls[k]).min()
                if minev <= 0:
                    log.warning("Sanity check failed: l%u is not pos. def.! Min. ev: %s", k, minev)
                    
                minev = la.eigvalsh(self.rs[k]).min()
                if minev <= 0:
                    log.warning("Sanity check failed: r%u is not pos. def.! Min. ev: %s", k, minev)
                
                norm = m.adot(self.ls[k], self.rs[k])
                if not np.allclose(norm, 1.0, atol=1E-13, rtol=0):
                    log.warning("Sanity check failed: Bad norm = %s", norm)
    
    def restore_SCF(self, ret_g=False, zero_tol=None):
        """Restores symmetric canonical form.
        
        In this canonical form, self.l == self.r and are diagonal matrices
        with the Schmidt coefficients corresponding to the half-chain
        decomposition form the diagonal entries.
        
        Parameters
        ----------
        ret_g : bool
            Whether to return the gauge-transformation matrices used.
            
        Returns
        -------
        g, g_i : ndarray
            Gauge transformation matrix g and its inverse g_i.
        """
        if zero_tol is None:
            zero_tol = self.zero_tol
        
        for k in xrange(self.L):
            X, Xi = tm.herm_fac_with_inv(self.rs[k], lower=True, zero_tol=zero_tol,
                                         force_evd=False,
                                         sanity_checks=self.sanity_checks, sc_data='Restore_SCF: r%u' % k)
            
            Y, Yi = tm.herm_fac_with_inv(self.ls[k], lower=False, zero_tol=zero_tol,
                                         force_evd=False,
                                         sanity_checks=self.sanity_checks, sc_data='Restore_SCF: l%u' % k)          
            
            U, sv, Vh = la.svd(Y.dot(X))
            
            #s contains the Schmidt coefficients,
            lam = sv**2
            self.S_hcs[k] = - np.sum(lam * sp.log2(lam))
            
            S = m.simple_diag_matrix(sv, dtype=self.typ)
            Srt = S.sqrt()
            
            g = m.mmul(Srt, Vh, Xi)
            
            g_i = m.mmul(Yi, U, Srt)
            
            j = (k + 1) % self.L
            for s in xrange(self.q):
                self.As[j][s] = g.dot(self.As[j][s])
                self.As[k][s] = self.As[k][s].dot(g_i)
                    
            if self.sanity_checks:
                Sfull = np.asarray(S)
                
                if not np.allclose(g.dot(g_i), np.eye(self.D)):
                    log.warning("Sanity check failed! Restore_SCF, bad GT! Off by %s",
                                la.norm(g.dot(g_i) - np.eye(self.D)))
                
                l = m.mmul(m.H(g_i), self.ls[k], g_i)
                r = m.mmul(g, self.rs[k], m.H(g))
                
                if not np.allclose(Sfull, l):
                    log.warning("Sanity check failed: Restore_SCF, left failed! Off by %s",
                                la.norm(Sfull - l))
                    
                if not np.allclose(Sfull, r):
                    log.warning("Sanity check failed: Restore_SCF, right failed! Off by %s",
                                la.norm(Sfull - r))
                
                l = Sfull
                for j in (sp.arange(k + 1, k + self.L + 1) % self.L):
                    l = tm.eps_l_noop(l, self.As[j], self.As[j])
                r = Sfull
                for j in (sp.arange(k, k - self.L, -1) % self.L):
                    r = tm.eps_r_noop(r, self.As[j], self.As[j])
                
                if not np.allclose(Sfull, l, rtol=self.itr_rtol*self.check_fac, 
                                   atol=self.itr_atol*self.check_fac):
                    log.warning("Sanity check failed: Restore_SCF, left %u bad! Off by %s",
                                k, la.norm(Sfull - l))
                    
                if not np.allclose(Sfull, r, rtol=self.itr_rtol*self.check_fac, 
                                   atol=self.itr_atol*self.check_fac):
                    log.warning("Sanity check failed: Restore_SCF, right %u bad! Off by %s",
                                k, la.norm(Sfull - r))
    
            self.ls[k] = S
            self.rs[k] = S
        
        if ret_g:
            return g, g_i
        else:
            return
    
    def restore_RCF(self, ret_g=False, zero_tol=None):
        """Restores right canonical form.
        
        In this form, self.r = sp.eye(self.D) and self.l is diagonal, with
        the squared Schmidt coefficients corresponding to the half-chain
        decomposition as eigenvalues.
        
        Parameters
        ----------
        ret_g : bool
            Whether to return the gauge-transformation matrices used.
            
        Returns
        -------
        g, g_i : ndarray
            Gauge transformation matrix g and its inverse g_i.
        """
        assert False, "Not yet supported!"
        
        if zero_tol is None:
            zero_tol = self.zero_tol
        
        #First get G such that r = eye
        G, G_i, rank = tm.herm_fac_with_inv(self.r, lower=True, zero_tol=zero_tol,
                                            return_rank=True,
                                            sanity_checks=self.sanity_checks,
                                            sc_data='Restore_RCF: r')

        self.l = m.mmul(m.H(G), self.l, G)
        
        #Now bring l into diagonal form, trace = 1 (guaranteed by r = eye..?)
        ev, EV = la.eigh(self.l)

        G = G.dot(EV)
        G_i = m.H(EV).dot(G_i)
        
        for s in xrange(self.q):
            self.A[s] = m.mmul(G_i, self.A[s], G)
            
        #ev contains the squares of the Schmidt coefficients,
        self.S_hc = - np.sum(ev * sp.log2(ev))
        
        self.l = m.simple_diag_matrix(ev, dtype=self.typ)
        
        r_old = self.r
        
        if rank == self.D:
            self.r = m.eyemat(self.D, self.typ)
        else:
            self.r = sp.zeros((self.D), dtype=self.typ)
            self.r[-rank:] = 1
            self.r = m.simple_diag_matrix(self.r, dtype=self.typ)

        if self.sanity_checks:            
            r_ = m.mmul(G_i, r_old, m.H(G_i)) 
            
            if not np.allclose(self.r, r_, 
                               rtol=self.itr_rtol*self.check_fac,
                               atol=self.itr_atol*self.check_fac):
                log.warning("Sanity check failed: Restore_RCF, bad r (bad GT).Off by %s", 
                            la.norm(r_ - self.r))
            
            l = tm.eps_l_noop(self.l, self.A, self.A)
            r = tm.eps_r_noop(self.r, self.A, self.A)
            
            if not np.allclose(r, self.r,
                               rtol=self.itr_rtol*self.check_fac, 
                               atol=self.itr_atol*self.check_fac):
                log.warning("Sanity check failed: Restore_RCF, r not eigenvector! Off by %s", 
                            la.norm(r - self.r))

            if not np.allclose(l, self.l,
                               rtol=self.itr_rtol*self.check_fac, 
                               atol=self.itr_atol*self.check_fac):
                log.warning("Sanity check failed: Restore_RCF, l not eigenvector! Off by %s", 
                            la.norm(l - self.l))
        
        if ret_g:
            return G, G_i
        else:
            return
    
    def restore_CF(self, ret_g=False):
        """Restores canonical form.
        
        Performs self.restore_RCF() or self.restore_SCF()
        depending on self.symm_gauge.        
        """
        if self.symm_gauge:
            return self.restore_SCF(ret_g=ret_g)
        else:
            return self.restore_RCF(ret_g=ret_g)
    
    def calc_AA(self):
        """Calculates the products A[s] A[t] for s, t in range(self.q).
        The result is stored in self.AA.
        """
        for k in xrange(len(self.As)):
            self.AAs[k] = tm.calc_AA(self.As[k], self.As[(k + 1) % self.L])
        
        
    def update(self, restore_CF=True, auto_truncate=False, restore_CF_after_trunc=True):
        """Updates secondary quantities to reflect the state parameters self.A.
        
        Must be used after changing the parameters self.A before calculating
        physical quantities, such as expectation values.
        
        Also (optionally) restores the right canonical form.
        
        Parameters
        ----------
        restore_CF : bool (True)
            Whether to restore canonical form.
        auto_truncate : bool (True)
            Whether to automatically truncate the bond-dimension if
            rank-deficiency is detected. Requires restore_CF.
        restore_CF_after_trunc : bool (True)
            Whether to restore_CF after truncation.
        """
        assert restore_CF or not auto_truncate, "auto_truncate requires restore_CF"
        
        self.calc_lr()
        if restore_CF:
            self.restore_CF()
            if auto_truncate and self.auto_truncate(update=False):
                log.info("Auto-truncated! New D: %d", self.D)
                self.calc_lr()
                if restore_CF_after_trunc:
                    self.restore_CF()
                
        self.calc_AA()
            
    def expect_1s(self, op, n):
        """Computes the expectation value of a single-site operator.
        
        The operator should be a self.q x self.q matrix or generating function 
        such that op[s, t] or op(s, t) equals <s|op|t>.
        
        The state must be up-to-date -- see self.update()!
        
        Parameters
        ----------
        op : ndarray or callable
            The operator.
            
        Returns
        -------
        expval : floating point number
            The expectation value (data type may be complex)
        """        
        if callable(op):
            op = np.vectorize(op, otypes=[np.complex128])
            op = np.fromfunction(op, (self.q, self.q))
            
        Or = tm.eps_r_op_1s(self.rs[n], self.As[n], self.As[n], op)
        
        return m.adot(self.ls[n - 1], Or)
        
    def expect_1s_1s(self, op1, op2, n, d):
        """Computes the expectation value of two single site operators acting 
        on two different sites.
        
        The result is < op1_n op2_n+d > with the operators acting on sites
        n and n + d.
        
        See expect_1s().
        
        Requires d > 0.
        
        The state must be up-to-date -- see self.update()!
        
        Parameters
        ----------
        op1 : ndarray or callable
            The first operator, acting on the first site.
        op2 : ndarray or callable
            The second operator, acting on the second site.
        d : int
            The distance (number of sites) between the two sites acted on non-trivially.
            
        Returns
        -------
        expval : floating point number
            The expectation value (data type may be complex)
        """        
        
        assert d > 0, 'd must be greater than 1'
        
        if callable(op1):
            op1 = sp.vectorize(op1, otypes=[sp.complex128])
            op1 = sp.fromfunction(op1, (self.q, self.q))
        
        if callable(op2):
            op2 = sp.vectorize(op2, otypes=[sp.complex128])
            op2 = sp.fromfunction(op2, (self.q, self.q)) 
        
        r_n = tm.eps_r_op_1s(self.rs[n], self.As[n], self.As[n], op2)

        for k in xrange(1, d):
            r_n = tm.eps_r_noop(r_n, self.As[(n - k) % self.L], self.As[(n - k) % self.L])

        r_n = tm.eps_r_op_1s(r_n, self.As[(n - d) % self.L], self.As[(n - d) % self.L], op1)
         
        return m.adot(self.ls[(n - d - 1) % self.L], r_n)
            
    def expect_2s(self, op, n):
        """Computes the expectation value of a nearest-neighbour two-site operator.
        
        The operator should be a q x q x q x q array 
        such that op[s, t, u, v] = <st|op|uv> or a function of the form 
        op(s, t, u, v) = <st|op|uv>.
        
        The state must be up-to-date -- see self.update()!
        
        Parameters
        ----------
        op : ndarray or callable
            The operator array or function.
            
        Returns
        -------
        expval : floating point number
            The expectation value (data type may be complex)
        """
        if callable(op):
            op = np.vectorize(op, otypes=[np.complex128])
            op = np.fromfunction(op, (self.q, self.q, self.q, self.q))        
        
        C = tm.calc_C_mat_op_AA(op, self.AAs[n])
        res = tm.eps_r_op_2s_C12_AA34(self.rs[n], C, self.AAs[n])
        
        return m.adot(self.ls[n - 1], res)
        
    def expect_3s(self, op, n):
        """Computes the expectation value of a nearest-neighbour three-site operator.

        The operator should be a q x q x q x q x q x q 
        array such that op[s, t, u, v, w, x] = <stu|op|vwx> 
        or a function of the form op(s, t, u, v, w, x) = <stu|op|vwx>.

        The state must be up-to-date -- see self.update()!

        Parameters
        ----------
        op : ndarray or callable
            The operator array or function.
            
        Returns
        -------
        expval : floating point number
            The expectation value (data type may be complex)
        """
        A = self.As[n]
        As = self.As
        AAA = tm.calc_AAA(As[n], As[(n + 1) % self.L], A[(n + 2) % self.L])

        if callable(op):
            op = sp.vectorize(op, otypes=[sp.complex128])
            op = sp.fromfunction(op, (A.shape[0], A.shape[0], A.shape[0],
                                      A.shape[0], A.shape[0], A.shape[0]))

        C = tm.calc_C_3s_mat_op_AAA(op, AAA)
        res = tm.eps_r_op_3s_C123_AAA456(self.rs[(n + 2) % self.L], C, AAA)
        return m.adot(self.ls[n - 1], res)
        
    def density_1s(self, n):
        """Returns a reduced density matrix for a single site.
        
        The site number basis is used: rho[s, t] 
        with 0 <= s, t < q.
        
        The state must be up-to-date -- see self.update()!
            
        Returns
        -------
        rho : ndarray
            Reduced density matrix in the number basis.
        """
        rho = np.empty((self.q, self.q), dtype=self.typ)
        for s in xrange(self.q):
            for t in xrange(self.q):                
                rho[s, t] = m.adot(self.ls[n - 1], m.mmul(self.As[n][t], self.rs[n], m.H(self.As[n][s])))
        return rho
                
    def apply_op_1s(self, op, n, do_update=True):
        """Applies a single-site operator to all sites.
        
        This applies the product (or string) of a single-site operator o_n over 
        all sites so that the new state |Psi'> = ...o_(n-1) o_n o_(n+1)... |Psi>.
        
        By default, this performs self.update(), which also restores
        state normalization.
        
        Parameters
        ----------
        op : ndarray or callable
            The single-site operator. See self.expect_1s().
        do_update : bool
            Whether to update after applying the operator.
        """
        if callable(op):
            op = np.vectorize(op, otypes=[np.complex128])
            op = np.fromfunction(op, (self.q, self.q))
            
        newA = sp.zeros_like(self.As[n])
        
        for s in xrange(self.q):
            for t in xrange(self.q):
                newA[s] += self.As[n][t] * op[s, t]
                
        self.As[n] = newA
        
        if do_update:
            self.update()         
                
    def set_q(self, newq):
        """Alter the single-site Hilbert-space dimension q.
        
        Any added parameters are set to zero.
        
        Parameters
        ----------
        newq : int
            The new dimension.
        """
        oldq = self.q
        oldAs = self.As
        
        oldls = self.ls
        oldrs = self.rs
        
        self._init_arrays(self.D, newq, self.L) 
        
        self.ls = oldls
        self.rs = oldrs
        
        for A in self.As:
            A.fill(0)
        for k in xrange(self.L):
            if self.q > oldq:
                self.As[k][:oldq, :, :] = oldAs[k]
            else:
                self.As[k][:] = oldAs[k][:self.q, :, :]
        
            
    def expand_D(self, newD, refac=100, imfac=0):
        """Expands the bond dimension in a simple way.
        
        New matrix entries are (mostly) randomized.
        
        Parameters
        ----------
        newD : int
            The new bond-dimension.
        refac : float
            Scaling factor for the real component of the added noise.
        imfac : float
            Scaling factor for the imaginary component of the added noise.
        """
        if newD < self.D:
            return False
        
        oldD = self.D
        oldAs = self.As
        
        oldls = np.asarray(self.ls)
        oldrs = np.asarray(self.rs)
        
        self._init_arrays(newD, self.q, self.L)
        
        for k in xrange(self.L):
            realnorm = la.norm(oldAs[k].real.ravel())
            imagnorm = la.norm(oldAs[k].imag.ravel())
            realfac = (realnorm / oldAs[k].size) * refac
            imagfac = (imagnorm / oldAs[k].size) * imfac
    #        m.randomize_cmplx(newA[:, self.D:, self.D:], a=-fac, b=fac)
            m.randomize_cmplx(self.As[k][:, :oldD, oldD:], a=0, b=realfac, aj=0, bj=imagfac)
            m.randomize_cmplx(self.As[k][:, oldD:, :oldD], a=0, b=realfac, aj=0, bj=imagfac)
            self.As[k][:, oldD:, oldD:] = 0 #for nearest-neighbour hamiltonian
    
    #        self.A[:, :oldD, oldD:] = oldA[:, :, :(newD - oldD)]
    #        self.A[:, oldD:, :oldD] = oldA[:, :(newD - oldD), :]
            self.As[k][:, :oldD, :oldD] = oldAs
    
            self.ls[k][:oldD, :oldD] = oldls[k]
            val = abs(oldls[k].mean())
            m.randomize_cmplx(self.ls[k].ravel()[oldD**2:], a=0, b=val, aj=0, bj=0)
            #self.l[:oldD, oldD:].fill(0 * 1E-3 * la.norm(oldl) / oldD**2)
            #self.l[oldD:, :oldD].fill(0 * 1E-3 * la.norm(oldl) / oldD**2)
            #self.l[oldD:, oldD:].fill(0 * 1E-3 * la.norm(oldl) / oldD**2)
            
            self.rs[k][:oldD, :oldD] = oldrs[k]
            val = abs(oldrs[k].mean())
            m.randomize_cmplx(self.rs[k].ravel()[oldD**2:], a=0, b=val, aj=0, bj=0)
            #self.r[oldD:, :oldD].fill(0 * 1E-3 * la.norm(oldr) / oldD**2)
            #self.r[:oldD, oldD:].fill(0 * 1E-3 * la.norm(oldr) / oldD**2)
            #self.r[oldD:, oldD:].fill(0 * 1E-3 * la.norm(oldr) / oldD**2)
