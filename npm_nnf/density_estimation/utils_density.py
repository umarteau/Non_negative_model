import numpy as np
import torch
import npm_nnf.utils.utils_kernels as KT
import matplotlib.pyplot as plt
import npm_nnf.utils.ppm as ppm
import pickle

torch.set_default_dtype(torch.float64)

############################################################################################
#Cholesky decompositions
############################################################################################

def produceDU(useGPU = False):
    if useGPU:
        def aux(x):
            return x.to('cpu')
        def aux2(x):
            return x.to('cuda')

        return (aux,aux2)
    else:
        def aux(x):
            return x
        return (aux,aux)


def chol(M,useGPU = False):
    m = M.size(0)
    if useGPU:
        Mg = M.to('cuda')
        Tg = Mg.cholesky(upper = True)
        T = Tg.to('cpu')
        del Mg
        del Tg
        return T
    else:
        T = M.cholesky(upper = True)
        return T    
    
    
    
def createT(kern,C,useGPU = False):
    K = kern(C,None)
    m = K.size(0)
    eps = 1e-15*m
    K[range(m),range(m)]+= eps
    T = chol(K,useGPU = useGPU)
    del K
    return T


def small_stopper(l_iter,tol = 1e-2,d = 5):
    if len(l_iter) < 10:
        return False
    else:
        n = len(l_iter)
        k = n //d
        max_shift = l_iter[n-1] - l_iter[k*(d-1)]
        max_shift_2 = l_iter[n-1] - l_iter[3]
        r = max_shift/max_shift_2
        #print(r)
        if r < tol:
            return True
        else:
            return False

class integral_tracker(object):
    def __init__(self,tol = 1e-2,d = 6):
        self.tol = tol
        self.d = d
        self.count = 0
        self.count_1 = 0

    def add_int(self,val):
        self.count += 1
        if val < 1+ self.tol and val > 1-self.tol :
            self.count_1 += 1
        else:
            self.count_1 = 0
    def check_int(self):
        if self.count_1/self.count > 1/self.d and self.count > 30:
            return True

        else:
            return False



    
############################################################################################
#Solving triangular systems
############################################################################################

def tr_solve(x,T,useGPU = False,transpose = False):
    m = T.size(0)
    if useGPU:
        download,upload = produceDU(useGPU = useGPU)
        xg = upload(x)
        Tg = upload(T)
        resg = torch.triangular_solve(xg,Tg,upper \
                        = True,transpose = transpose)
        res = download(resg[0])
        del xg
        del Tg
        del resg
        torch.cuda.empty_cache()
        return res
    else:
        return torch.triangular_solve(x,T,upper \
                                                   = True,transpose = transpose)[0]
        
        
def add_l(a,b,c):
    if a == None:
        if type(b) == torch.Tensor:
            return c*b
        else:
            try:
                return [add_l(None,bb,c) for bb in b]
            except:
                print("shit")
                return c*b
    if type(b) == torch.Tensor:
        return a + c*b
    try:
        return [add_l(aa,b[i],c) for i,aa in enumerate(a)]
    except:
        return a+c*b
    
def minus_l(b):
    return add_l(None,b,-1)

def scal_prod(a,b):
    if type(b) == torch.Tensor:
        return torch.sum(a*b)
    try:
        res = 0
        for i,aa in enumerate(a):
            res += scal_prod(aa,b[i]) 
        return res
    except:
        return torch.sum(a*b)

########################################
#Linear Model
#######################################

class LMK(object): 
    def __init__(self,sigma,x,kernel = 'gaussian',centered = False,c = 0,base = '1'
                 ,mu_base = None,eta_base = None,useGPU = False,nmax_gpu = None,target_norm = 1):
        n = x.size(0)
        if x.ndim == 1:
            d = 1
        else:
            d = x.size(1)
    
        self.n = n
        self.x = x
        self.d = d
        self.target_norm = target_norm
        self.useGPU = useGPU
        
        if kernel == 'expo':
            kernel_fun = KT.expoKernel(sigma)
        elif kernel == 'gaussian':
            kernel_fun = KT.gaussianKernel(sigma)
            
        def kern_aux(A,B):
            return KT.blockKernComp(A, B, kernel_fun, useGPU = useGPU, nmax_gpu = nmax_gpu)
        if centered == False:
            def kern(A):
                return kern_aux(x,A)+c
        else:
            K_0 = kern_aux(x,None)
            K_0m = K_0.mean(1)
            K_0mm = K_0m.mean()
            def kern(A):
                K_a = kern_aux(x,A)
                K_am = K_a.mean(0)
                K_a -= K_am.unsqueeze(0).expand_as(K_a)
                K_a -= K_0m.unsqueeze(1).expand_as(K_a)
                K_a += K_0mm +c
                return K_a
        self.kern = kern
        
        K =  kern(None)
        K[range(n),range(n)]+= 1e-12*n
        self.renorm = [1,1]
        self.renorm[0] = torch.sqrt(2*(K[range(n),range(n)]).sum()/n)/target_norm
        self.V = chol(K,useGPU = useGPU)
        
        
        
        if kernel == 'gaussian':
            iv = KT.integrateGuaussianVector(sigma,base = base,mu_base = mu_base,eta_base = eta_base)
        elif kernel == 'expo':
            iv = KT.integrateExpoVector(sigma,base = base,mu_base = mu_base,eta_base = eta_base)
        
        o = torch.ones((n,1))
        kt = iv(x).view(n,1)
        
        if c > 0 and base == '1':
            raise NameError("Model not integrable, c > 0 and base is lebesgue")
            
        if centered: 
            ko = kern_aux(x,None)@o
            coef = -kt.T @ o/n + c + ko.T@o/n**2
            Sig = kt - ko/n + coef*o
        else:
            Sig = kt +c*o
        
        Sig = tr_solve(Sig,self.V,useGPU = useGPU,transpose = True)
        
        self.renorm[1] = torch.sqrt(2*(Sig**2).sum())/target_norm
        
        self.Sig = Sig.view((1,n))

        
        if base == '1':
            def nu(x):
                return torch.zeros(x.size(0)) +1
            self.nu = nu
        elif base == 'gaussian':
            def nu(x):
                if type(mu_base) != torch.Tensor:
                    mu_b = torch.tensor(mu_base).view(d)
                else:
                    mu_b = mu_base.view(d)
                if x.ndim > 1:
                    res = torch.exp(-((x-mu_b.unsqueeze(0))**2).sum(1)/(2*eta_base**2)-d*np.log(2*np.pi*eta_base**2)/2)
                else:
                    res = torch.exp(-(x-mu_b)**2/(2*eta_base**2)-d*np.log(2*np.pi*eta_base**2)/2)
                return res.view(x.size(0))
            self.nu = nu
        
        
        
        def dz():
            return [torch.zeros((n,1)),torch.zeros((1,1))]
        
        self.dz = dz

    
    
    def R(self,a):
        n = self.n
        vals = (self.V).T @ a/(np.sqrt(n)*self.renorm[0])
        equality = self.Sig @ a/self.renorm[1]
        return [vals,equality]
            
    def Rt(self,dv):
        n = dv[0].size(0)
        t1 = self.V@ dv[0]/(np.sqrt(n)*self.renorm[0])
        t2 = dv[1]*self.Sig.T/self.renorm[1]
        return t1 + t2

    def integral(self,a):
        return self.Sig @ a
        
    def Rx(self,a,xtest):
        print("integral = {}".format(self.Sig @ a))
        n = self.n
        Ktt = self.kern(xtest)
        bid = tr_solve(Ktt,self.V,useGPU = self.useGPU,transpose = True)
        return (bid.T @ a).view(xtest.size(0)) 
    
    def px(self,a,xtest):
        return self.Rx(a,xtest)*(self.nu(xtest).view(xtest.size(0)))


class LMK2(object):
    def __init__(self, sigma, x, kernel='gaussian', centered=False, c=0, base='1'
                 , mu_base=None, eta_base=None, useGPU=False, nmax_gpu=None, target_norm=1):
        n = x.size(0)
        if x.ndim == 1:
            d = 1
        else:
            d = x.size(1)

        self.n = n
        self.x = x
        self.d = d
        self.target_norm = target_norm
        self.useGPU = useGPU

        if kernel == 'expo':
            kernel_fun = KT.expoKernel(sigma)
        elif kernel == 'gaussian':
            kernel_fun = KT.gaussianKernel(sigma)

        def kern_aux(A, B):
            return KT.blockKernComp(A, B, kernel_fun, useGPU=useGPU, nmax_gpu=nmax_gpu)

        if centered == False:
            def kern(A):
                return kern_aux(x, A) + c
        else:
            K_0 = kern_aux(x, None)
            K_0m = K_0.mean(1)
            K_0mm = K_0m.mean()

            def kern(A):
                K_a = kern_aux(x, A)
                K_am = K_a.mean(0)
                K_a -= K_am.unsqueeze(0).expand_as(K_a)
                K_a -= K_0m.unsqueeze(1).expand_as(K_a)
                K_a += K_0mm + c
                return K_a
        self.kern = kern

        K = kern(None)
        K[range(n), range(n)] += 1e-12 * n
        self.renorm = [1, 1]
        self.renorm[0] = torch.sqrt( (K[range(n), range(n)]).sum() / n)
        self.V = chol(K, useGPU=useGPU)

        if kernel == 'gaussian':
            iv = KT.integrateGuaussianVector(sigma, base=base, mu_base=mu_base, eta_base=eta_base)
        elif kernel == 'expo':
            iv = KT.integrateExpoVector(sigma, base=base, mu_base=mu_base, eta_base=eta_base)

        o = torch.ones((n, 1))
        kt = iv(x).view(n, 1)

        if c > 0 and base == '1':
            raise NameError("Model not integrable, c > 0 and base is lebesgue")

        if centered:
            ko = kern_aux(x, None) @ o
            coef = -kt.T @ o / n + c + ko.T @ o / n ** 2
            Sig = kt - ko / n + coef * o
        else:
            Sig = kt + c * o

        Sig = tr_solve(Sig, self.V, useGPU=useGPU, transpose=True)

        self.renorm[1] = torch.sqrt(2 * (Sig ** 2).sum())

        self.bigRenorm = torch.sqrt((Sig ** 2).sum())

        self.contraint = Sig.view((1,n))/self.bigRenorm

        self.Sig = Sig.view((1, n))

        if base == '1':
            def nu(x):
                return torch.zeros(x.size(0)) + 1

            self.nu = nu
        elif base == 'gaussian':
            def nu(x):
                if type(mu_base) != torch.Tensor:
                    mu_b = torch.tensor(mu_base).view(d)
                else:
                    mu_b = mu_base.view(d)
                if x.ndim > 1:
                    res = torch.exp(-((x - mu_b.unsqueeze(0)) ** 2).sum(1) / (2 * eta_base ** 2) - d * np.log(
                        2 * np.pi * eta_base ** 2) / 2)
                else:
                    res = torch.exp(-(x - mu_b) ** 2 / (2 * eta_base ** 2) - d * np.log(2 * np.pi * eta_base ** 2) / 2)
                return res.view(x.size(0))

            self.nu = nu

        def dz():
            return [torch.zeros((n, 1)), torch.zeros((1, 1))]

        self.dz = dz

    def R(self, a):
        n = self.n
        vals = (self.V).T @ a / (np.sqrt(n) * self.renorm[0])
        equality = self.constraint @ a / self.renorm[1]
        return [vals, equality]

    def Rt(self, dv):
        n = dv[0].size(0)
        t1 = self.V @ dv[0] / (np.sqrt(n) * self.renorm[0])
        t2 = dv[1] * self.constraint.T / self.renorm[1]
        return t1 + t2

    def integral(self, a):
        return self.Sig @ a

    def Rx(self, a, xtest):
        print("integral = {}".format(self.constraint @ a))
        n = self.n
        Ktt = self.kern(xtest)
        bid = tr_solve(Ktt, self.V, useGPU=self.useGPU, transpose=True)
        return (bid.T @ a).view(xtest.size(0))/self.bigRenorm

    def px(self, a, xtest):
        return self.Rx(a, xtest) * (self.nu(xtest).view(xtest.size(0)))


class Sreg(object):
    def __init__(self,la):
        self.smoothness = 1/la
        self.la = la
        
        
        
    def Omega(self,f):
        return 0.5*self.la*((f**2).sum())
    
    def Omegas(self):    
        def fun_only(f):
            return 0.5*self.smoothness*(f**2).sum()
        def fun_grad(f):
            return (0.5*self.smoothness*(f**2).sum(),self.smoothness*f)
        return fun_only,fun_grad
    
    def recoverPrimal(self,f):
        return self.smoothness*f




class LinearEstimator(object):
    def __init__(self,la = 1,sigma = 1,Niter = 100,score_param = 'normal',kernel = 'gaussian',centered = False,c = 0,
                 base = 'gaussian',mu_base = None,eta_base = None,target_norm = 1):

        self.la = la
        self.sigma = sigma
        self.Niter = Niter
        self.score_param = score_param
        self.kernel = kernel
        self.centered = centered
        self.c = c
        self.base = base
        self.mu_base = mu_base
        self.eta_base = eta_base
        self.target_norm = target_norm

    def get_params(self, deep=True):
        # suppose this estimator has parameters "alpha" and "recursive"
        return {"la": self.la, "sigma": self.sigma,"Niter" : self.Niter,
                "score_param": self.score_param,
                "kernel" : self.kernel,
                "centered" : self.centered,
                "c" : self.c,
                "base" : self.base,
                "mu_base" : self.mu_base,
                "eta_base" : self.eta_base,
                "target_norm" : self.target_norm
                }

    def set_params(self, **parameters):
        for parameter, value in parameters.items():
            setattr(self, parameter, value)
        return self

    def fit(self,X,y = None):
        print(f'sigma = {self.sigma}, lambda = {self.la}')
        self.Xtrain = X
        self.regmodel = Sreg(self.la)
        self.lmodel = LMK2(self.sigma,X,kernel = self.kernel,centered = self.centered,c = self.c,base = self.base
                 ,mu_base = self.mu_base,eta_base = self.eta_base,target_norm = self.target_norm)
        self.dModel = densityModel(self.regmodel, self.lmodel)
        if not(isinstance(self.Niter,int)):
            freq = int(100 + 10*np.sqrt(1/self.dModel.reg.la)) // 5
        else:
            freq = self.Niter // 5
        cb, cobj = self.dModel.cbcboj_pd(freq, plot=False)
        al = self.dModel.prox_method(self.Niter, cb=cb, cobj=cobj)
        self.al = al


    def predict(self,X,y = None):
        return self.dModel.px_dual(self.al, X)


    def score(self,X,y = None):
        p = self.dModel.px_dual(self.al, X)
        if self.score_param == 'normal':
            if (p <= 0).sum() > 0:
                return -torch.tensor(np.inf)
            else:
                return (torch.log(p)).mean()




    
class QKM(object): 
    def __init__(self,sigma,x,kernel = 'gaussian',centered = False,c = 0,base = '1',mu_base = None,eta_base = None,useGPU = False,nmax_gpu = None,target_norm = 1):
        if c > 0 and base == '1':
            raise NameError("Model not integrable, c > 0 and base is lebesgue")
        
        n = x.size(0)
        if x.ndim == 1:
            d = 1
        else:
            d = x.size(1)
        self.d = d
        self.n = n
        self.x = x
        self.target_norm = target_norm
        self.useGPU = useGPU
        
        if kernel == 'expo':
            kernel_fun = KT.expoKernel(sigma)
        elif kernel == 'gaussian':
            kernel_fun = KT.gaussianKernel(sigma)
            
        def kern_aux(A,B):
            return KT.blockKernComp(A, B, kernel_fun, useGPU = useGPU, nmax_gpu = nmax_gpu)
        if centered == False:
            def kern(A):
                return kern_aux(x,A)+c
        else:
            K_0 = kern_aux(x,None)
            K_0m = K_0.mean(1)
            K_0mm = K_0m.mean()
            def kern(A):
                K_a = kern_aux(x,A)
                K_am = K_a.mean(0)
                K_a -= K_am.unsqueeze(0).expand_as(K_a)
                K_a -= K_0m.unsqueeze(1).expand_as(K_a)
                K_a += K_0mm +c
                return K_a
        self.kern = kern
        
        K =  kern(None)
        K[range(n),range(n)]+= 1e-12*n
        self.renorm = [1,1]
        self.renorm[0] = torch.sqrt(2*(K[range(n),range(n)]**2).sum()/n)/target_norm
        self.V = chol(K,useGPU = useGPU)
        
        if base == '1' and c > 0:
            raise NameError("Model not integrable, c > 0 and base is lebesgue")
        
        
        if kernel == 'gaussian':
            k1_fun = KT.integrateGuaussianVector(sigma,base = base,mu_base = mu_base,eta_base = eta_base)
            k2_fun = KT.integrateGuaussianMatrix(sigma,base = base,mu_base = mu_base,eta_base = eta_base)
        if kernel == 'expo':
            k1_fun = KT.integrateExpoVector(sigma,base = base,mu_base = mu_base,eta_base = eta_base)
            k2_fun = KT.integrateExpoMatrix(sigma,base = base,mu_base = mu_base,eta_base = eta_base)
        
        K2 = KT.blockKernComp(x, None, k2_fun, useGPU = useGPU, nmax_gpu = nmax_gpu)
        
        if centered == False:
            Sig =  K2  + c
        
        else:
            vv = torch.ones((n,1))/n
            K0 = kern_aux(x,None)
            vK1 = k1_fun(x).view(n,1)
            vK0 = K0@vv
            vK2 = K2@vv
            cK0 = vv.T@vK0
            cK1 = vv.T@vK1
            cK2 = vv.T @ vK2
            
            Mbig = K2 - vK1@vK0.T - vK0@vK1.T + vK0@vK0.T
            vbig = (cK1*vK0 + cK0*vK1 - cK0*vK0 - vK2).expand((n,n))
            cbig = cK2 - 2*cK1*cK0 + cK0*cK0
            
            Sig = Mbig + vbig + vbig.T + cbig
        
        
        Sigg = tr_solve(Sig,self.V,useGPU = useGPU,transpose = True)
        Sig = tr_solve(Sigg.T,self.V,useGPU = useGPU,transpose = True).T
        
        self.renorm[1] = torch.sqrt(2*(Sig**2).sum())/target_norm
        
        self.Sig = Sig
    

            
        if base == '1':
            def nu(x):
                return 0*x +1
            self.nu = nu
        elif base == 'gaussian':
            def nu(x):
                if type(mu_base) != torch.Tensor:
                    mu_b = torch.tensor(mu_base).view(d)
                else:
                    mu_b = mu_base.view(d)
                if x.ndim > 1:
                    res = (torch.exp(-((x-mu_b.unsqueeze(0))**2).sum(1)/(2*eta_base**2)-d*np.log(2*np.pi*eta_base**2)/2)).view(x.size(0))
                else:
                    res = (torch.exp(-(x-mu_b)**2/(2*eta_base**2)-d*np.log(2*np.pi*eta_base**2)/2)).view(x.size(0))
                return res
            self.nu = nu
        
        def dz():
            return [torch.zeros((n,1)),torch.zeros((1,1))]
        
        self.dz = dz
        
        return None
    
    def R(self,f):
        res = self.dz()
        n = self.n
        D,U = produceDU(useGPU = self.useGPU)
        Vg = U(self.V)
        fg = U(f)
        res[0] = D((Vg * (fg @ Vg)).sum(0)).view(n,1)/(np.sqrt(n)*self.renorm[0])
        res[1] = ((fg*U(self.Sig)).sum()).view(1,1)/(self.renorm[1])
            
        return res
        
    def Rt(self,alpha):
        n = self.n
        D,U = produceDU(useGPU = self.useGPU)
        Vg = U(self.V)
        return (D(Vg @ (U(alpha[0].view(n)) * Vg).T)/(np.sqrt(self.n)*self.renorm[0])+alpha[1]*self.Sig/self.renorm[1]) 

        
    def integral(self,f):
        return (self.Sig * f).sum()

    def Rx(self,f,xtest):
        print("integral = {}".format((self.Sig * f).sum()))
        Ktt = self.kern(xtest)
        bid = tr_solve(Ktt,self.V,useGPU = self.useGPU,transpose = True)
        return ((bid * (f@bid)).sum(0)).view(xtest.size(0))
    
    def px(self,a,xtest):
        return self.Rx(a,xtest)*(self.nu(xtest).view(xtest.size(0)))


class QKM2(object):
    def __init__(self, sigma, x, kernel='gaussian', centered=False, c=0, base='1', mu_base=None, eta_base=None,
                 useGPU=False, nmax_gpu=None, target_norm=1):
        if c > 0 and base == '1':
            raise NameError("Model not integrable, c > 0 and base is lebesgue")

        n = x.size(0)
        if x.ndim == 1:
            d = 1
        else:
            d = x.size(1)
        self.d = d
        self.n = n
        self.x = x
        self.target_norm = target_norm
        self.useGPU = useGPU

        if kernel == 'expo':
            kernel_fun = KT.expoKernel(sigma)
        elif kernel == 'gaussian':
            kernel_fun = KT.gaussianKernel(sigma)

        def kern_aux(A, B):
            return KT.blockKernComp(A, B, kernel_fun, useGPU=useGPU, nmax_gpu=nmax_gpu)

        if centered == False:
            def kern(A):
                return kern_aux(x, A) + c
        else:
            K_0 = kern_aux(x, None)
            K_0m = K_0.mean(1)
            K_0mm = K_0m.mean()

            def kern(A):
                K_a = kern_aux(x, A)
                K_am = K_a.mean(0)
                K_a -= K_am.unsqueeze(0).expand_as(K_a)
                K_a -= K_0m.unsqueeze(1).expand_as(K_a)
                K_a += K_0mm + c
                return K_a
        self.kern = kern

        K = kern(None)
        K[range(n), range(n)] += 1e-12 * n
        self.renorm = [1, 1]
        self.renorm[0] = torch.sqrt(2 * (K[range(n), range(n)] ** 2).sum() / n) / target_norm
        self.V = chol(K, useGPU=useGPU)

        if base == '1' and c > 0:
            raise NameError("Model not integrable, c > 0 and base is lebesgue")

        if kernel == 'gaussian':
            k1_fun = KT.integrateGuaussianVector(sigma, base=base, mu_base=mu_base, eta_base=eta_base)
            k2_fun = KT.integrateGuaussianMatrix(sigma, base=base, mu_base=mu_base, eta_base=eta_base)
        if kernel == 'expo':
            k1_fun = KT.integrateExpoVector(sigma, base=base, mu_base=mu_base, eta_base=eta_base)
            k2_fun = KT.integrateExpoMatrix(sigma, base=base, mu_base=mu_base, eta_base=eta_base)

        K2 = KT.blockKernComp(x, None, k2_fun, useGPU=useGPU, nmax_gpu=nmax_gpu)

        if centered == False:
            Sig = K2 + c

        else:
            vv = torch.ones((n, 1)) / n
            K0 = kern_aux(x, None)
            vK1 = k1_fun(x).view(n, 1)
            vK0 = K0 @ vv
            vK2 = K2 @ vv
            cK0 = vv.T @ vK0
            cK1 = vv.T @ vK1
            cK2 = vv.T @ vK2

            Mbig = K2 - vK1 @ vK0.T - vK0 @ vK1.T + vK0 @ vK0.T
            vbig = (cK1 * vK0 + cK0 * vK1 - cK0 * vK0 - vK2).expand((n, n))
            cbig = cK2 - 2 * cK1 * cK0 + cK0 * cK0

            Sig = Mbig + vbig + vbig.T + cbig

        Sigg = tr_solve(Sig, self.V, useGPU=useGPU, transpose=True)
        Sig = tr_solve(Sigg.T, self.V, useGPU=useGPU, transpose=True).T

        self.renorm[1] = np.sqrt(2 ) / target_norm

        self.Sig = Sig

        self.bigRenorm = torch.sqrt((Sig**2).sum())
        self.constraint = self.Sig/self.bigRenorm
        if base == '1':
            def nu(x):
                return 0 * x + 1

            self.nu = nu
        elif base == 'gaussian':
            def nu(x):
                if type(mu_base) != torch.Tensor:
                    mu_b = torch.tensor(mu_base).view(d)
                else:
                    mu_b = mu_base.view(d)
                if x.ndim > 1:
                    res = (torch.exp(-((x - mu_b.unsqueeze(0)) ** 2).sum(1) / (2 * eta_base ** 2) - d * np.log(
                        2 * np.pi * eta_base ** 2) / 2)).view(x.size(0))
                else:
                    res = (torch.exp(
                        -(x - mu_b) ** 2 / (2 * eta_base ** 2) - d * np.log(2 * np.pi * eta_base ** 2) / 2)).view(
                        x.size(0))
                return res

            self.nu = nu

        def dz():
            return [torch.zeros((n, 1)), torch.zeros((1, 1))]

        self.dz = dz

        return None

    def R(self, f):
        res = self.dz()
        n = self.n
        D, U = produceDU(useGPU=self.useGPU)
        Vg = U(self.V)
        fg = U(f)
        res[0] = D((Vg * (fg @ Vg)).sum(0)).view(n, 1) / (np.sqrt(n) * self.renorm[0])
        res[1] = ((fg * U(self.constraint)).sum()).view(1, 1) / (self.renorm[1])

        return res

    def Rt(self, alpha):
        n = self.n
        D, U = produceDU(useGPU=self.useGPU)
        Vg = U(self.V)
        return (D(Vg @ (U(alpha[0].view(n)) * Vg).T) / (np.sqrt(self.n) * self.renorm[0]) + alpha[1] * self.constraint /
                self.renorm[1])

    def integral(self, f):
        return (self.constraint * f).sum()

    def Rx(self, f, xtest):
        print("integral = {}".format((self.constraint * f).sum()))
        print(f'renorm size {self.bigRenorm}')
        Ktt = self.kern(xtest)
        bid = tr_solve(Ktt, self.V, useGPU=self.useGPU, transpose=True)
        return ((bid * (f @ bid)).sum(0)).view(xtest.size(0))/self.bigRenorm

    def px(self, a, xtest):
        return self.Rx(a, xtest) * (self.nu(xtest).view(xtest.size(0)))


class ENreg(object):
    def __init__(self,mu,la,useGPU = False):
        self.smoothness = 1/la
        self.mu = mu
        self.la = la
        def aux(A):
            #return ppm.sureApproach(A,mu=mu,useGPU = useGPU)
            return ppm.mixedApproach(A, 150, q=6, mu = mu, useGPU = useGPU)
        self.pp = aux
        
    def Omega(self,f):
        return self.mu * torch.trace(f) + 0.5*self.la*((f**2).sum())
    
    def Omegas(self):    
        def fun_only(f):
            fpos = self.pp(f)
            return 0.5*self.smoothness*(fpos**2).sum()
        def fun_grad(f):
            fpos =self.pp(f)
            return (0.5*self.smoothness*(fpos**2).sum(),self.smoothness*fpos)
        return fun_only,fun_grad
    
    def recoverPrimal(self,f):
        fpos =self.pp(f)
        return (self.smoothness*fpos)
 

    
class logLikelihoodConstrained(object):
    def __init__(self,renorm):
        self.ka = renorm[0]
        self.eq = 1/renorm[1]
        return None
    def L(self,alpha):
        ka = self.ka
        eq = self.eq
        n = alpha[0].size(0)
        if alpha[1] - eq > 1e-4 or alpha[1] - eq < - 1e-4 :
            return torch.tensor(np.inf)
        else:
            return torch.mean(-torch.log(np.sqrt(n)*ka*alpha[0]))
    
    def Ls(self,alpha):
        ka = self.ka
        eq = self.eq
        n = alpha[0].size(0)
        return (-1 + alpha[1]*eq  + torch.mean(-torch.log(-np.sqrt(n)*alpha[0]/ka)))
    
    def Lsprox(self,c,alpha):
        ka = self.ka
        eq = self.eq
        n = alpha[0].size(0)
        def aux_prox(x):
            return 0.5*(x + torch.sqrt(x**2 + 4 * c/ka**2))
        res = []
        res.append( (-ka/(np.sqrt(n)))*aux_prox(-np.sqrt(n)*alpha[0]/ka))
        res.append(alpha[1]-c*eq)
        return res    
    
    
class densityModel(object):
    def __init__(self,reg,lmodel,is_plot = True):
        self.reg = reg
        self.lmodel = lmodel
        self.loss = logLikelihoodConstrained(lmodel.renorm)
        self.smoothness = self.reg.smoothness*self.lmodel.target_norm**2
        self.is_plot = is_plot
    
    def F_primal(self,B):
        return self.loss.L(self.lmodel.R(B)) + self.reg.Omega(B)
    
    def F_dual(self,alpha):
        return -(self.loss.Ls(alpha) + self.reg.Omegas()[0](self.lmodel.Rt(minus_l(alpha))))
    
    def pfd(self,alpha):
        return self.reg.recoverPrimal(minus_l(self.lmodel.Rt(alpha)))
    
    def F_primald(self,alpha):
        return self.F_primal(self.pfd(alpha))
        
        
    def Rx_dual(self,alpha,xtest):
        B = self.pfd(alpha)
        return self.lmodel.Rx(B,xtest)
    
    def px_dual(self,alpha,xtest):
        B = self.pfd(alpha)
        return self.lmodel.px(B,xtest)

    def integral_dual(self,alpha):
        B = self.pfd(alpha)
        return self.lmodel.integral(B)
    
    def cb_prox(cobj,al):
        return None
    
    def cbcboj_pd(self,freq,plot= False):
        cobj = {}
        cobj['it'] = 0
        cobj['primal'] = []
        cobj['dual'] = []
        cobj['itfreq']=[]
        def cb(cobj,al):
            if cobj['it']%freq == 0:
                print("---iteration: {}---".format(cobj['it']+1))
                cobj['primal'].append(self.F_primald(al))
                cobj['dual'].append(self.F_dual(al))
                cobj['itfreq'].append(cobj['it'])
                if plot:
                    plt.semilogy(cobj['itfreq'],np.array(cobj['primal']) - np.array(cobj['dual']))
                    plt.xlabel("iterations")
                    plt.ylabel("dual gap")
                    plt.show()
                cobj['it'] +=1
            else:
                cobj['it'] +=1
        return cb,cobj
    
    def cbTest(self,xtest,freq,name = "",print_it = False):
        def tLoss(alpha):
            ratio = self.Rx_dual(alpha,xtest)
            return torch.mean(-torch.log(ratio)) 
        def cb(cobj,al):
            if not(name in cobj.keys()):
                if not(print_it):
                    print("starting "+ name)
                cobj['it'] = 0
                cobj['itfreq'] = []
                cobj[name] = []
            if cobj['it']%freq == 0:
                if print_it:
                    print(name + "---iteration: {}---".format(cobj['it']+1))
                cobj[name].append(tLoss(al))
                cobj['itfreq'].append(cobj['it'])
                cobj['it'] +=1
            else:
                cobj['it'] +=1
        return cb
        
    
    def prox_method(self,Niter,cb = cb_prox,cobj = {}):

        is_plot = self.is_plot

        if Niter == 'auto':
            is_auto = True
        else:
            is_auto = False
        if isinstance(Niter,type(None)) or isinstance(Niter,str):
            Niter_bis = int(1000 + 10*np.sqrt(1/self.reg.la))
        else:
            Niter_bis = Niter

        it = integral_tracker(tol = 1e-2)


    
        O_fun,O_fungrad = self.reg.Omegas()
    
        def Oms_dual(alpha):
            x = minus_l(self.lmodel.Rt(alpha))
            f_alpha,g_x = O_fungrad(x)
            g_alpha =  minus_l(self.lmodel.R(g_x))
            def Gl_dual(Lf,talpha):
                dd = add_l(talpha,alpha,-1)
                f_approx = f_alpha + scal_prod(g_alpha,dd) + 0.5*Lf*scal_prod(dd,dd)
                f_alphat = O_fun(self.lmodel.Rt(minus_l(talpha)))
                return f_approx-f_alphat
            return f_alpha,g_alpha,Gl_dual

        
        al = self.lmodel.dz()
        al2 = self.lmodel.dz()
    
        tk = 1
    
        loss_iter = []
    
        Lf = 0.05*self.smoothness
        eta_Lf = 1.1
        
        #Lmax = ka
        for i in range(Niter_bis):
            Oval,Ograd,Gl_dual = Oms_dual(al2)
            if i > 0:
                f,g = O_fungrad(self.lmodel.Rt(minus_l(al)))
                loss_iter.append(-self.loss.Ls(al) - f)
                it.add_int(self.lmodel.integral(g))
            while True:
                c = 1/Lf
                al1 = self.loss.Lsprox(c,add_l(al2,Ograd,-c))
                if Gl_dual(Lf,al1) >=0 or Lf >= 2*eta_Lf*self.smoothness:

                    break
                else:
                    Lf *= eta_Lf
            tk1 = (1 + np.sqrt(1+4*tk**2))/2
            th = (tk - 1)/(tk1)
            al2 = add_l(al1,add_l(al1,al,-1),th)
            al = al1
            tk = tk1
            cb(cobj,al)
            if is_auto:
                sst = small_stopper(loss_iter,tol = 1e-3,d = 5)
                #if sst:
                    #print(f"Finished after {i} iterations")
                    #break
                if sst and it.check_int():
                    print(f"Finished after {i} iterations")
                    break

        print(f'Integral tracker values : {it.count},{it.count_1}')
        if is_plot:
            plt.plot(list(range(len(loss_iter))),(loss_iter))
            plt.show()
        return(al)




class QuadraticEstimator(object):
    def __init__(self,la = 1,sigma = 1,Niter = None,score_param = 'normal',
                 mu = None,kernel = 'gaussian',centered = False,c = 0,
                 base = 'gaussian',mu_base = None,eta_base = None,is_plot = False,x_train = None,y_train = None,
                 al = None):
        self.la = la
        self.sigma = sigma
        self.Niter = Niter
        self.score_param = score_param
        self.mu = mu
        self.kernel = kernel
        self.centered = centered
        self. c = c
        self.base = base
        self.mu_base = mu_base
        self.eta_base = eta_base
        self.is_plot = is_plot
        self.x_train = x_train
        self.y_train = y_train
        self.al = al





    def get_params(self, deep=True):
        # suppose this estimator has parameters "alpha" and "recursive"
        return {"la": self.la, "sigma": self.sigma,"Niter" : self.Niter,"score_param": self.score_param,
                "mu" : self.mu,
                "kernel" : self.kernel,
                "centered" : self.centered,
                "c" : self.c,
                "base" : self.base,
                "mu_base" : self.mu_base,
                "eta_base" : self.eta_base,
                "is_plot" : self.is_plot,
                "al" : self.al,
                "x_train" : self.x_train,
                "y_train" : self.y_train
                }

    def set_params(self, **parameters):
        for parameter, value in parameters.items():
            setattr(self, parameter, value)
        return self

    def fit(self,X,y = None):

        if isinstance(self.mu,type(None)):
            self.mu = self.la*0.01
        print(f'sigma = {self.sigma}, lambda = {self.la}, mu = {self.mu}')


        self.target_norm = np.sqrt(self.mu)
        self.lmodel = QKM2(self.sigma, X, kernel=self.kernel, centered=self.centered,
                           c=self.c, base=self.base, mu_base=self.mu_base,
                           eta_base=self.eta_base, target_norm=self.target_norm)
        self.regmodel = ENreg(self.la, self.mu)
        self.dModel = densityModel(self.regmodel, self.lmodel,is_plot = self.is_plot)
        al = self.dModel.prox_method(self.Niter)
        self.al = al
        self.x_train = X
        self.y_train = y


    def predict(self,X,y = None):
        return self.dModel.px_dual(self.al, X)


    def score(self,X,y = None):
        p = self.dModel.px_dual(self.al, X)
        integral = self.dModel.integral_dual(self.al)
        if integral > 1.1 or integral < 0.9:
            return np.nan
        if self.score_param == 'normal':
            if (p <= 0).sum() > 0:
                return np.nan
            else:
                return (torch.log(p)).mean()
    def save(self,filename):
        res = {"la": self.la, "sigma": self.sigma, "Niter": self.Niter, "score_param": self.score_param,
         "mu": self.mu,
         "kernel": self.kernel,
         "centered": self.centered,
         "c": self.c,
         "base": self.base,
         "mu_base": self.mu_base,
         "eta_base": self.eta_base,
         "is_plot": self.is_plot,
         "al": self.al,
         "x_train": self.x_train,
         "y_train": self.y_train
         }
        pickle.dump(res,open(filename,"wb"))
    def load(self,filename = None):
        if not(isinstance(filename,type(None))):
            p = pickle.load(open(filename, "rb"))
            for parameter, value in p.items():
                setattr(self, parameter, value)
        if isinstance(self.mu,type(None)):
            self.mu = self.la*0.01
        self.target_norm = np.sqrt(self.mu)
        self.lmodel = QKM2(self.sigma, self.x_train, kernel=self.kernel, centered=self.centered,
                           c=self.c, base=self.base, mu_base=self.mu_base,
                           eta_base=self.eta_base, target_norm=self.target_norm)
        self.regmodel = ENreg(self.la, self.mu)
        self.dModel = densityModel(self.regmodel, self.lmodel,is_plot = self.is_plot)



        

###########################################################
#NW method
############################################################
            

            

def logloss(x):
    return -(torch.log(x)).sum()

def logloss_prox(eps,x):
    return (x+ torch.sqrt(x**2 + 4*eps))/2

class loglossNW(object):
    def __init__(self,eps):
        self.eps = eps
        self.L = logloss
        self.proxL = logloss_prox
        return None
    
    def Leps(self,alpha):
        eps = self.eps
        y = self.proxL(eps,alpha)
        return self.L(y) + ((alpha-y)**2).sum()/(2*eps)
    
    def gradLeps(self,alpha):
        eps = self.eps
        return (alpha - self.proxL(eps,alpha))/eps
    
    
class squareloss(object):
    def __init__(self,y):
        self.y = y
        return None
    
    def Leps(self,alpha):
        return 0.5*((((self.y).view(alpha.size()))- alpha)**2).sum()
    
    def gradLeps(self,alpha):
        return (alpha - (self.y).view(alpha.size()))
    


class kernelModel(object):
    def __init__(self,sigma,x,kernel = 'gaussian',c = 0,base = '1',mu_base = None,eta_base = None,useGPU = False,nmax_gpu = None):
        n = x.size(0)
        if x.ndim == 1:
            d = 1
        else:
            d = x.size(1)
    
        self.n = n
        self.x = x
        self.d = d
        self.useGPU = useGPU
        
        if kernel == 'expo':
            kernel_fun = KT.expoKernel(sigma)
        elif kernel == 'gaussian':
            kernel_fun = KT.gaussianKernel(sigma)
            
        def kern_aux(A,B):
            return KT.blockKernComp(A, B, kernel_fun, useGPU = useGPU, nmax_gpu = nmax_gpu)
        def kern(A):
            return kern_aux(x,A)+c
        self.kern = kern
        
        
        K =  kern(None)
        #K[range(n), range(n)] += 1e-12 * n
        self.K = K
        self.ka = (K[range(n),range(n)]).sum()/n

        
        if base == '1':
            def nu(x):
                return 0*x +1
            self.nu = nu
        elif base == 'gaussian':
            def nu(x):
                if type(mu_base) != torch.Tensor:
                    mu_b = torch.tensor(mu_base).view(d)
                else:
                    mu_b = mu_base.view(d)
                if x.ndim > 1:
                    res = torch.exp(-((x-mu_b.unsqueeze(0))**2).sum(1)/(2*eta_base**2)-d*np.log(2*np.pi*eta_base**2)/2).view(x.size(0))
                else:
                    res = torch.exp(-(x-mu_b)**2/(2*eta_base**2)-d*np.log(2*np.pi*eta_base**2)/2).view(x.size(0))
                return res
            self.nu = nu
        
        if kernel == 'gaussian':
            iv = KT.integrateGuaussianVector(sigma,base = base,mu_base = mu_base,eta_base = eta_base)
        elif kernel == 'expo':
            iv = KT.integrateExpoVector(sigma,base = base,mu_base = mu_base,eta_base = eta_base)
        
        o = torch.ones((n,1))
        kt = iv(x).view(n,1)
        
        if c > 0 and base == '1':
            raise NameError("Model not integrable, c > 0 and base is lebesgue")
            
        Sig = kt +c*o
        self.Sig = Sig
        print(f' norm of the constraint : {torch.sqrt((Sig**2).sum())}')
        self.renorm = torch.sqrt((Sig**2).sum())

        
        def dz():
            return torch.zeros((n,1))
        self.dz = dz
    def integral(self,a):
        return self.Sig.T @ a/self.renorm
        
    def Rx(self,a,xtest):
        print("integral = {}".format(self.Sig.T @ a/self.renorm))
        n = self.n
        Ktt = self.kern(xtest)
        return (Ktt.T @ a/self.renorm).view(xtest.size(0))
    
    def px(self,a,xtest):
        return (self.Rx(a,xtest)*(self.nu(xtest).view(xtest.size(0)))).view(xtest.size(0))
        

            

class densityModelNW(object):
    def __init__(self,kmodel,la,eps = 0.001,is_plot = True):
        self.kmodel = kmodel
        n = kmodel.n
        self.lossmodel = loglossNW(eps)
        self.la = la
        
        self.c = la*kmodel.n
        self.constraint = kmodel.Sig/kmodel.renorm
        self.smoothness = n**2 * kmodel.ka**2/eps + n**2 * kmodel.ka*la
        self.is_plot = is_plot
        
    def proj(self,x):
        a = self.constraint
        n = x.size(0)
        u = x/a
        us,ind = torch.sort(u,dim = 0,descending = True)
        a2s = (a**2)[ind.view(n),:]
        uasc = (us*a2s).cumsum(0)
        a2sc = a2s.cumsum(0)
    
        A = uasc-a2sc*us
    
        k = 0
    
        while k< n-1  and A[k+1,0]<1  :
            k+=1

        la = (uasc[k,0] -1)/a2sc[k,0]

        return torch.clamp(x-la*a,min = 0)
    
    def Loss_tot(self,x):
        K = self.kmodel.K
        return self.c*x.T @ K @ x /2 + self.lossmodel.Leps(K@x)
    
    def grad(self,x):
        K = self.kmodel.K
        grad = self.c*K@x + K@self.lossmodel.gradLeps(K@x)
        return grad
    
    def cb_prox(cobj,al):
        return None
    
    def FISTA(self,Niter,cb = cb_prox,cobj = {}):
        is_plot = self.is_plot
        if Niter == 'auto':
            is_auto = True
        else:
            is_auto = False
        if isinstance(Niter,type(None)) or isinstance(Niter,str):
            Niter_bis = int(100 + 10*np.sqrt(self.smoothness))
        else:
            Niter_bis = Niter

        it = integral_tracker(tol = 1e-2)
    

        def Gl(alpha):
            f_alpha = self.Loss_tot(alpha)
            g_alpha = self.grad(alpha)
            def aux(Lf,talpha):
                dd = talpha- alpha
                f_approx = f_alpha + g_alpha.T @ dd + 0.5*Lf*(dd**2).sum()
                f_alphat = self.Loss_tot(talpha)
                return f_approx-f_alphat
            return f_alpha,g_alpha,aux

        
        al = self.kmodel.dz()
        al2 = self.kmodel.dz()
    
        tk = 1
    
        loss_iter = []
    
        Lf = 0.01*self.smoothness
        eta_Lf = 1.1
        
        #Lmax = ka
        for i in range(Niter_bis):
            fun,grad,GL = Gl(al2)
            if i > 0:
                loss_iter.append(self.Loss_tot(al))
                it.add_int(self.kmodel.integral(al))
            while True:
                gamma = 1/Lf
                al1 = self.proj(al2-gamma*grad)
                if GL(Lf,al1) >=0 :
                    #or Lf >= self.smoothness
                    break
                else:
                    Lf *= eta_Lf 
            #print('Lf/Lmax = {}'.format(Lf/self.smoothness))
            tk1 = (1 + np.sqrt(1+4*tk**2))/2
            th = (tk - 1)/(tk1)
            al2 = al1 + th*(al1-al)
            al = al1
            tk = tk1
            cb(cobj,al)
            if is_auto:
                sst = small_stopper(loss_iter,tol = 1e-3,d = 5)
                if sst and it.check_int():
                    print(f"Finished after {i} iterations")
                    break
        if is_plot:
            plt.plot(list(range(len(loss_iter))),(loss_iter))
            plt.show()
        return(al)
        
        
class funApproxModelNW(object):
    def __init__(self,kmodel,la,y,is_plot = True):
        self.kmodel = kmodel
        n = kmodel.n
        self.lossmodel = squareloss(y)
        self.la = la
        
        self.c = la*kmodel.n
        self.smoothness = n**2 * kmodel.ka**2 + n**2 * kmodel.ka*la
        self.is_plot = is_plot
        
    def proj(self,x):
    
        return torch.clamp(x,min = 0)
    
    def Loss_tot(self,x):
        K = self.kmodel.K
        return self.c*x.T @ K @ x /2 + self.lossmodel.Leps(K@x)
    
    def grad(self,x):
        K = self.kmodel.K
        grad = self.c*K@x + K@self.lossmodel.gradLeps(K@x)
        return grad
    
    def cb_prox(cobj,al):
        return None
    
    def FISTA(self,Niter,cb = cb_prox,cobj = {}):
        is_plot = self.is_plot
    

        def Gl(alpha):
            f_alpha = self.Loss_tot(alpha)
            g_alpha = self.grad(alpha)
            def aux(Lf,talpha):
                dd = talpha- alpha
                f_approx = f_alpha + g_alpha.T @ dd + 0.5*Lf*(dd**2).sum()
                f_alphat = self.Loss_tot(talpha)
                return f_approx-f_alphat
            return f_alpha,g_alpha,aux

        
        al = self.kmodel.dz()
        al2 = self.kmodel.dz()
    
        tk = 1
    
        loss_iter = []
    
        Lf = 0.01*self.smoothness
        eta_Lf = 1.1
        
        #Lmax = ka
        for i in range(Niter):
            fun,grad,GL = Gl(al2)
            if i > 0:
                loss_iter.append(self.Loss_tot(al))
            while True:
                gamma = 1/Lf
                al1 = self.proj(al2-gamma*grad)
                print("al1size = {}".format(al1.size()))
                print("al2size = {}".format(al2.size()))
                print("gradsize = {}".format(grad.size()))
                if GL(Lf,al1) >=0 :
                    #or Lf >= self.smoothness
                    break
                else:
                    Lf *= eta_Lf 
            #print('Lf/Lmax = {}'.format(Lf/self.smoothness))
            tk1 = (1 + np.sqrt(1+4*tk**2))/2
            th = (tk - 1)/(tk1)
            al2 = al1 + th*(al1-al)
            al = al1
            tk = tk1
            cb(cobj,al)

        if is_plot:
            plt.plot(list(range(len(loss_iter))),(loss_iter))
            plt.show()
        return(al)



class NadarayaWatsonEstimator(object):
    def __init__(self,la = 1,sigma = 1,Niter = None,score_param = 'normal',kernel = 'gaussian',c = 0,
                 base = 'gaussian',mu_base = None,eta_base = None,eps = 0.001,is_plot = False):
        self.la = la
        self.sigma = sigma
        self.Niter = Niter
        self.score_param = score_param
        self.kernel = kernel
        self.c = c
        self.base = base
        self.mu_base = mu_base
        self.eta_base = eta_base
        self.eps = eps
        self.is_plot = is_plot



    def get_params(self, deep=True):
        # suppose this estimator has parameters "alpha" and "recursive"
        return {"la": self.la, "sigma": self.sigma,"Niter" : self.Niter,"score_param": self.score_param,
                "kernel" : self.kernel,
                "c" : self.c,
                "base" : self.base,
                "mu_base" : self.mu_base,
                "eta_base" : self.eta_base,
                "eps" : self.eps,
                "is_plot" : self.is_plot
                }

    def set_params(self, **parameters):
        for parameter, value in parameters.items():
            setattr(self, parameter, value)
        return self

    def fit(self,X,y = None):
        print(f'sigma = {self.sigma}, lambda = {self.la}')
        self.kmodel = kernelModel(self.sigma, X, kernel=self.kernel, c=self.c, base=self.base, mu_base=self.mu_base,
                                  eta_base=self.eta_base)
        self.densityModel = densityModelNW(self.kmodel, self.la, eps= self.eps,is_plot=self.is_plot)


        al = self.densityModel.FISTA(self.Niter)
        self.al = al


    def predict(self,X,y = None):
        return self.kmodel.px(self.al, X)


    def score(self,X,y = None):
        p = self.kmodel.px(self.al, X)
        if self.score_param == 'normal':
            if (p <= 0).sum() > 0:
                return -torch.tensor(np.inf)
            else:
                return (torch.log(p)).mean()

########################################################
#
#########################################################

class kernelExpoModel(object):
    def __init__(self,sigma,x,rgrid,ngrid,cgrid = None,kernel = 'gaussian',centered = False,c = 0,base = '1',mu_base = None,eta_base = None,useGPU = False,nmax_gpu = None):
        n = x.size(0)
        self.n = n
        if x.ndim == 1:
            d =1
        else:
            d = x.size(1)
        self.d = d
        
        if kernel == 'expo':
            kernel_fun = KT.expoKernel(sigma)
        elif kernel == 'gaussian':
            kernel_fun = KT.gaussianKernel(sigma)
            
        def kern_aux(A,B):
            return KT.blockKernComp(A, B, kernel_fun, useGPU = useGPU, nmax_gpu = nmax_gpu)
        if centered == False:
            def kern(A):
                return kern_aux(x,A)+c
        else:
            K_0 = kern_aux(x,None)
            K_0m = K_0.mean(1)
            K_0mm = K_0m.mean()
            def kern(A):
                K_a = kern_aux(x,A)
                K_am = K_a.mean(0)
                K_a -= K_am.unsqueeze(0).expand_as(K_a)
                K_a -= K_0m.unsqueeze(1).expand_as(K_a)
                K_a += K_0mm +c
                return K_a
        self.kern = kern
        
        K =  kern(None)
        self.K = K
        self.ka = np.sqrt((K[range(n),range(n)]).sum())
        K[range(n),range(n)]+= 1e-12*n
        self.V = chol(K,useGPU = useGPU)
        self.useGPU = useGPU
        self.nmax_gpu = nmax_gpu
        
        if cgrid == 0 or isinstance(cgrid,type(None)):
            cgrid = torch.zeros((ngrid,d))
        else:
            cgrid = cgrid.view((1,d))
            cgrid = cgrid.expand((ngrid,d))
        xgrid = 2*rgrid*torch.rand((ngrid,d))-rgrid + cgrid
        self.ngrid = ngrid
        self.xgrid = xgrid
        #self.lgrid = xgrid[1]-xgrid[0]
        self.lgrid = (2*rgrid)**d/ngrid
        Kgrid =  kern(xgrid)
        self.Vgrid = tr_solve(Kgrid,self.V,useGPU = self.useGPU,transpose = True)
        def dz():
            return torch.zeros((n,1))
        self.dz = dz
        
    def R(self,a):
        return self.V.T@a
        
    def Rx(self,a,xtest):
        n = self.n
        Ktt = self.kern(xtest)
        bid = tr_solve(Ktt,self.V,useGPU = self.useGPU,transpose = True)
        return (bid.T @ a).view(xtest.size(0))
    
    def pdata(self,a):
        V = self.V
        n = self.n
        ggrid = a.T@self.Vgrid
        mggrid = ggrid.max()
        ggrid -= mggrid
        num_p = torch.exp(ggrid)
        int_estimate = self.lgrid*num_p.sum()
        pdata = torch.exp(a.T@V - mggrid).view(n,1)/int_estimate
        return pdata
    
    def Jacobian(self,a):
        V = self.V
        n = self.n
        ggrid = a.T@self.Vgrid
        mggrid = ggrid.max()
        ggrid -= mggrid
        num_p = torch.exp(ggrid)
        int_estimate = self.lgrid*num_p.sum()
        expectation_estimate = ((self.lgrid*num_p.view(self.ngrid)*self.Vgrid).sum(1)).view(n)/int_estimate
        pdata = torch.exp(a.T@V - mggrid).view(n)/int_estimate
        return (V - expectation_estimate[:,None])*pdata[None,:],pdata.view(n,1)
        
        
    
    def px(self,a,xtest):
        V = self.V
        n = self.n
        ggrid = a.T@self.Vgrid
        mggrid = ggrid.max()
        ggrid -= mggrid
        num_p = torch.exp(ggrid)
        int_estimate = self.lgrid*num_p.sum()
        rx = self.Rx(a,xtest)-mggrid
        return(torch.exp(rx)/int_estimate)

class kernelExpoModelTer(object):
    def __init__(self,sigma,x,ngrid,kernel = 'gaussian',centered = False,c = 0,useGPU = False,nmax_gpu = None,base = 'gaussian',mu_base = 0,eta_base = 1):
        n = x.size(0)
        self.n = n
        if x.ndim == 1:
            d =1
        else:
            d = x.size(1)
        self.d = d
        
        if kernel == 'expo':
            kernel_fun = KT.expoKernel(sigma)
        elif kernel == 'gaussian':
            kernel_fun = KT.gaussianKernel(sigma)
            
        def kern_aux(A,B):
            return KT.blockKernComp(A, B, kernel_fun, useGPU = useGPU, nmax_gpu = nmax_gpu)
        if centered == False:
            def kern(A):
                return kern_aux(x,A)+c
        else:
            K_0 = kern_aux(x,None)
            K_0m = K_0.mean(1)
            K_0mm = K_0m.mean()
            def kern(A):
                K_a = kern_aux(x,A)
                K_am = K_a.mean(0)
                K_a -= K_am.unsqueeze(0).expand_as(K_a)
                K_a -= K_0m.unsqueeze(1).expand_as(K_a)
                K_a += K_0mm +c
                return K_a
        self.kern = kern
        
        K =  kern(None)
        self.K = K
        self.ka = np.sqrt((K[range(n),range(n)]).sum())
        K[range(n),range(n)]+= 1e-12*n
        self.V = chol(K,useGPU = useGPU)
        self.useGPU = useGPU
        self.nmax_gpu = nmax_gpu
        
        
        if type(mu_base) != torch.Tensor:
            mu_b = torch.tensor(mu_base).expand(d)
        else:
            mu_b = mu_base.view(d)
        
        if base == '1':

            volume = (2*eta_base)**d
            def nu(x):
                return (1 - ((x - mu_b.unsqueeze(0)) > eta_base).sum(1).double() - ((x - mu_b.unsqueeze(0)) < -eta_base).sum(1).double()).clamp(min=0)/volume
            self.nu = nu
            xgrid = mu_b.unsqueeze(0) + eta_base*(2*torch.rand((ngrid,d))-1)
        elif base == 'gaussian':
            def nu(x):
                if x.ndim > 1:
                    res = torch.exp(-((x-mu_b.unsqueeze(0))**2).sum(1)/(2*eta_base**2)-d*np.log(2*np.pi*eta_base**2)/2).view(x.size(0))
                else:
                    res = torch.exp(-(x-mu_b)**2/(2*eta_base**2)-d*np.log(2*np.pi*eta_base**2)/2).view(x.size(0))
                return res
            self.nu = nu
            eps = torch.randn((ngrid,d))
            xgrid = mu_b.unsqueeze(0)+eta_base * eps

        self.nudata = (self.nu(x)).view(n)
        self.ngrid = ngrid
        self.xgrid = xgrid
        #self.lgrid = xgrid[1]-xgrid[0]
        Kgrid =  kern(xgrid)
        self.Vgrid = tr_solve(Kgrid,self.V,useGPU = self.useGPU,transpose = True)
        def dz():
            return torch.zeros((n,1))
        self.dz = dz
        
    def R(self,a):
        return self.V.T@a
        
    def Rx(self,a,xtest):
        n = self.n
        Ktt = self.kern(xtest)
        bid = tr_solve(Ktt,self.V,useGPU = self.useGPU,transpose = True)
        return (bid.T @ a).view(xtest.size(0))
    
    def pdata(self,a):
        V = self.V
        n = self.n
        ggrid = a.T@self.Vgrid
        mggrid = ggrid.max()
        ggrid -= mggrid
        num_p = torch.exp(ggrid)
        int_estimate = num_p.mean()
        pdata = torch.exp(a.T@V - mggrid).view(n,1)*self.nudata.view(n,1)/int_estimate
        return pdata
    
    def Jacobian(self,a):
        V = self.V
        n = self.n
        ggrid = a.T@self.Vgrid
        mggrid = ggrid.max()
        ggrid -= mggrid
        num_p = torch.exp(ggrid)
        int_estimate = num_p.mean()
        expectation_estimate = ((num_p.view(self.ngrid)*self.Vgrid).mean(1)).view(n)/int_estimate
        pdata = torch.exp(a.T@V - mggrid).view(n)*self.nudata/int_estimate
        return (V - expectation_estimate[:,None])*pdata[None,:],pdata.view(n,1)
        
        
    
    def px(self,a,xtest):
        V = self.V
        n = self.n
        ggrid = a.T@self.Vgrid
        mggrid = ggrid.max()
        ggrid -= mggrid
        num_p = torch.exp(ggrid)
        int_estimate = num_p.mean()
        rx = self.Rx(a,xtest)-mggrid
        nux = self.nu(xtest)
        return(torch.exp(rx)*nux/int_estimate)

class kernelExpoModelBis(object):
    def __init__(self,sigma,x,ngrid,base_sampler,base_density,kernel = 'gaussian',centered = False,c = 0,useGPU = False,nmax_gpu = None):
        n = x.size(0)
        self.n = n
        if x.ndim == 1:
            d =1
        else:
            d = x.size(1)
        self.d = d
        
        if kernel == 'expo':
            kernel_fun = KT.expoKernel(sigma)
        elif kernel == 'gaussian':
            kernel_fun = KT.gaussianKernel(sigma)
            
        def kern_aux(A,B):
            return KT.blockKernComp(A, B, kernel_fun, useGPU = useGPU, nmax_gpu = nmax_gpu)
        if centered == False:
            def kern(A):
                return kern_aux(x,A)+c
        else:
            K_0 = kern_aux(x,None)
            K_0m = K_0.mean(1)
            K_0mm = K_0m.mean()
            def kern(A):
                K_a = kern_aux(x,A)
                K_am = K_a.mean(0)
                K_a -= K_am.unsqueeze(0).expand_as(K_a)
                K_a -= K_0m.unsqueeze(1).expand_as(K_a)
                K_a += K_0mm +c
                return K_a
        self.kern = kern
        
        K =  kern(None)
        self.K = K
        self.ka = np.sqrt((K[range(n),range(n)]).sum())
        K[range(n),range(n)]+= 1e-12*n
        self.V = chol(K,useGPU = useGPU)
        self.useGPU = useGPU
        self.nmax_gpu = nmax_gpu
        
        
        xgrid = base_sampler(ngrid)
        self.nu= base_density
        self.nudata = base_density(x).view(n)
        self.ngrid = ngrid
        self.xgrid = xgrid
        #self.lgrid = xgrid[1]-xgrid[0]
        Kgrid =  kern(xgrid)
        self.Vgrid = tr_solve(Kgrid,self.V,useGPU = self.useGPU,transpose = True)
        def dz():
            return torch.zeros((n,1))
        self.dz = dz
        
    def R(self,a):
        return self.V.T@a
        
    def Rx(self,a,xtest):
        n = self.n
        Ktt = self.kern(xtest)
        bid = tr_solve(Ktt,self.V,useGPU = self.useGPU,transpose = True)
        return (bid.T @ a).view(xtest.size(0))
    
    def pdata(self,a):
        V = self.V
        n = self.n
        ggrid = a.T@self.Vgrid
        mggrid = ggrid.max()
        ggrid -= mggrid
        num_p = torch.exp(ggrid)
        int_estimate = num_p.mean()
        pdata = torch.exp(a.T@V - mggrid).view(n,1)*self.nudata.view(n,1)/int_estimate
        return pdata
    
    def Jacobian(self,a):
        V = self.V
        n = self.n
        ggrid = a.T@self.Vgrid
        mggrid = ggrid.max()
        ggrid -= mggrid
        num_p = torch.exp(ggrid)
        int_estimate = num_p.mean()
        expectation_estimate = ((num_p.view(self.ngrid)*self.Vgrid).mean(1)).view(n)/int_estimate
        pdata = torch.exp(a.T@V - mggrid).view(n)*self.nudata/int_estimate
        return (V - expectation_estimate[:,None])*pdata[None,:],pdata.view(n,1)
        
        
    
    def px(self,a,xtest):
        V = self.V
        n = self.n
        ggrid = a.T@self.Vgrid
        mggrid = ggrid.max()
        ggrid -= mggrid
        num_p = torch.exp(ggrid)
        int_estimate = num_p.mean()
        rx = self.Rx(a,xtest)-mggrid
        nux = self.nu(xtest)
        return(torch.exp(rx)*nux/int_estimate)

class densityModelExpo(object):
    def __init__(self,kmodel,la,eps = 0.001):
        self.kmodel = kmodel
        n = kmodel.n
        self.lossmodel = loglossNW(eps)
        self.la = la
        
        self.c = la*kmodel.n
        self.smoothness =  kmodel.ka**2/eps + n*la
        
    
    def Loss_tot(self,x):
        return self.c*(x**2).sum()/2 + self.lossmodel.Leps(self.kmodel.pdata(x))
    
    def grad(self,x):
        J,pdata = self.kmodel.Jacobian(x)
        grad = self.c*x + J@self.lossmodel.gradLeps(pdata)
        return grad
    
    def cb_prox(cobj,al):
        return None
    
    def GD(self,Niter,cb = cb_prox,cobj = {},N_restarts = 3):

        l_restarts = []

        al = self.kmodel.dz()

        for kk in range(N_restarts):

            if Niter == 'auto':
                is_auto = True
            else:
                is_auto = False
            if isinstance(Niter,type(None)) or isinstance(Niter,str):
                Niter_bis = int(100 + 10*np.sqrt(self.smoothness))
            else:
                Niter_bis = Niter




            def Gl(alpha):
                f_alpha = self.Loss_tot(alpha)
                g_alpha = self.grad(alpha)
                def aux(Lf,talpha):
                    dd = talpha- alpha
                    f_approx = f_alpha + g_alpha.T @ dd + 0.5*Lf*(dd**2).sum()
                    f_alphat = self.Loss_tot(talpha)
                    return f_approx-f_alphat
                return f_alpha,g_alpha,aux



            al2 = al.clone()


            tk = 1

            loss_iter = []

            Lf = 0.000001*self.smoothness
            eta_Lf = 1.1

            #Lmax = ka
            for i in range(Niter_bis):
                fun,grad,GL = Gl(al2)
                if i > 0:
                    loss_iter.append(self.Loss_tot(al))
                while True:
                    gamma = 1/Lf
                    al1 = al2-gamma*grad
                    if GL(Lf,al1) >=0 :
                        #or Lf >= self.smoothness
                        break
                    else:
                        Lf *= eta_Lf
                #print('Lf/Lmax = {}'.format(Lf/self.smoothness))
                tk1 = (1 + np.sqrt(1+4*tk**2))/2
                th = (tk - 1)/(tk1)
                al2 = al1 + th*(al1-al)
                al = al1
                tk = tk1
                cb(cobj,al)
                if is_auto:
                    sst = small_stopper(loss_iter,tol = 1e-3,d = 5)
                    if sst :
                        print(f"Finished automatically after {i} iterations")
                        break

            plt.plot(list(range(len(loss_iter))),(loss_iter))
            plt.show()
            l_restarts.append((al.clone(),loss_iter[-1]))
        print([el[1] for el in l_restarts])
        return l_restarts[min(range(len(l_restarts)),key = lambda i : l_restarts[i][1])][0]






class ExpoEstimator(object):
    def __init__(self,la = 1,sigma = 1,rgrid = 4,ngrid = 100,cgrid = None, Niter = None, score_param = 'normal',
                 kernel = 'gaussian',centered = False,c = 0,
                 base = 'gaussian',mu_base = None,eta_base = None,eps = 0.001):
        self.la = la
        self.sigma = sigma
        self.Niter = Niter
        self.score_param = score_param
        self.kernel = kernel
        self.c = c
        self.base = base
        self.mu_base = mu_base
        self.eta_base = eta_base
        self.eps = eps
        self.rgrid = rgrid
        self.ngrid = ngrid
        self.cgrid = cgrid
        self.centered = centered
#        self.target_norm = np.sqrt(la * 0.01)



    def get_params(self, deep=True):
        # suppose this estimator has parameters "alpha" and "recursive"
        return {"la": self.la, "sigma": self.sigma,"Niter" : self.Niter,"score_param": self.score_param,
                "kernel" : self.kernel,
                "c" : self.c,
                "base" : self.base,
                "mu_base" : self.mu_base,
                "eta_base" : self.eta_base,
                "eps" : self.eps,
                "rgrid" : self.rgrid,
                "ngrid" : self.ngrid,
                "cgrid" : self.cgrid,
                "centered" : self.centered
                }

    def set_params(self, **parameters):
        for parameter, value in parameters.items():
            setattr(self, parameter, value)
        return self

    def fit(self,X,y = None):
        self.kmodel = kernelExpoModel(self.sigma,X,self.rgrid,self.ngrid,cgrid = self.cgrid,
                                            kernel = self.kernel ,centered = self.centered,c = self.c,
                                      base = self.base,mu_base = self.mu_base,eta_base = self.eta_base)

        self.densityModel = densityModelExpo(self.kmodel,self.la,eps=self.eps)


        al = self.densityModel.GD(self.Niter)

        self.al = al


    def predict(self,X,y = None):
        return self.kmodel.px(self.al, X)


    def score(self,X,y = None):
        p = self.kmodel.px(self.al, X)
        if self.score_param == 'normal':
            if (p <= 0).sum() > 0:
                return -torch.tensor(np.inf)
            else:
                return (torch.log(p)).mean()

