
import torch
import numpy as np

from Base.mcmc import MetropolisHastings,MCMCDA
from Base.lla import dgala

from nv_files.NavierStokes import Vorticity
from nv_files.Pseudo_Spectral_Solver import VorticitySolver2D
from nv_files.Field_Generator import omega0_samples_torch


class NVMCMC(MetropolisHastings):
    def __init__(self, surrogate, observation_locations, observations_values, nparameters=2, 
                 observation_noise=0.5, nsamples=1000000, burnin=None, proposal_type="random_walk", 
                 step_size=0.1, device="cpu"):
        
        super(NVMCMC, self).__init__(observation_locations, observations_values, nparameters, 
                 observation_noise, nsamples, burnin, proposal_type, step_size, device)
        
        self.surrogate = surrogate

        # Dictionary to map surrogate classes to their likelihood functions
        likelihood_methods = {Vorticity: self.nn_log_likelihood,
                                dgala: self.dgala_log_likelihood}

        # Precompute the likelihood function at initialization
        surrogate_type = type(surrogate)
        if surrogate_type in likelihood_methods:
            self.log_likelihood_func = likelihood_methods[surrogate_type]
        else:
            raise ValueError(f"Surrogate of type {surrogate_type.__name__} is not supported.")

    def log_prior(self, theta):
        if not ((theta >= -1) & (theta <= 1)).all():
            return -torch.inf
        else:
            return 0

    def nn_log_likelihood(self, theta):
        """
        Evaluates the log-likelihood given a NN.
        """
        data = torch.cat([self.observation_locations, theta.repeat(self.observation_locations.size(0), 1)], dim=1).float()
        surg = self.surrogate.w(data.float()).clone().detach()
        return -0.5 * torch.sum(((self.observations_values - surg) ** 2) / (self.observation_noise ** 2))

    def dgala_log_likelihood(self, theta):
        """
        Evaluates the log-likelihood given a dgala.
        """
        data = torch.cat([self.observation_locations, theta.repeat(self.observation_locations.size(0), 1)], dim=1).float()
        surg_mu, surg_sigma = self.surrogate(data)

        surg_mu = surg_mu[:,0].detach().view(-1, 1)
        surg_sigma = surg_sigma[:, 0].detach().view(-1, 1)

        sigma = self.observation_noise ** 2 + surg_sigma
        dy = surg_mu.shape[0]

        cte = 0.5 * (dy * torch.log(torch.tensor(2 * torch.pi)) + torch.sum(torch.log(sigma)))

        return -0.5 * torch.sum(((self.observations_values - surg_mu.reshape(-1, 1)) ** 2) / sigma)- cte
    
    def log_likelihood(self, theta):
        """Directly call the precomputed likelihood function."""
        return self.log_likelihood_func(theta)
        

class NVMCMCDA(MCMCDA):
    def __init__(self,coarse_surrogate,observation_locations, observations_values, nparameters=2, fs_indices_sol = None,
                 fs_n = 128, fs_T=2,fs_steps =5e-4,observation_noise=1e-3, iter_mcmc=1000000, iter_da = 20000,                 
                 proposal_type="random_walk", step_size=1e-3, device="cpu" ):
        
        super(NVMCMCDA, self).__init__(observation_locations, observations_values, nparameters, 
                 observation_noise, iter_mcmc, iter_da,proposal_type, step_size, device)
        
        self.coarse_surrogate = coarse_surrogate
        self.fs_indices_sol = fs_indices_sol
        self.finer_surrogate = VorticitySolver2D(N=fs_n, L=2*np.pi, T=fs_T, nu=1e-2, 
                                       dt=fs_steps,num_sol=2, method='CN', force= self.force_function)
        
        # Initialize the FEMSolver once, if numerical solver is used
        X = torch.linspace(0, 1, fs_n)*2*torch.pi  # Spatial grid in X direction
        Y = torch.linspace(0, 1, fs_n)*2*torch.pi  # Spatial grid in Y direction
        X, Y = torch.meshgrid(X, Y,indexing="ij")  # Create meshgrid for X, Y

        self.X = X.to(device)
        self.Y = Y.to(device)

        # Dictionary to map surrogate classes to likelihood functions
        likelihood_methods = {
            VorticitySolver2D: self.psm_log_likelihood,
            Vorticity: self.nn_log_likelihood,
            dgala: self.dgala_log_likelihood
        }

        # Precompute likelihood function for both surrogates
        self.log_likelihood_outer_func = self.get_likelihood_function(coarse_surrogate, likelihood_methods)
        self.log_likelihood_inner_func = self.psm_log_likelihood

    def force_function(self,X,Y):
        return  (np.sin(X + Y) + np.cos(X + Y))
    
    def log_prior(self, theta):
        if not ((theta >= -1) & (theta <= 1)).all():
            return -torch.inf
        else:
            return 0

    def psm_log_likelihood(self, theta ):
        """
        Evaluates the log-likelihood given a FEM.
        """
        theta = theta.reshape(1,-1).unsqueeze(-1) if self.nparameters==2 else theta.reshape(-1,2).unsqueeze(-1)

        w0 = omega0_samples_torch(self.X, self.Y, theta, d=5, tau=np.sqrt(2))

        surg = self.finer_surrogate.run_simulation(np.array(w0[:,:,0]))

        surg = torch.tensor(surg[-1], device=self.device).reshape(-1,1)
        surg = surg[self.fs_indices_sol]
        return -0.5 * torch.sum(((self.observations_values - surg) ** 2) / (self.observation_noise ** 2))

    def nn_log_likelihood(self,surrogate,theta):
        """
        Evaluates the log-likelihood given a NN.
        """
        data = torch.cat([self.observation_locations, theta.repeat(self.observation_locations.size(0), 1)], dim=1).float()
        surg = surrogate.w(data.float()).clone().detach()
        return -0.5 * torch.sum(((self.observations_values - surg) ** 2) / (self.observation_noise ** 2))

    def dgala_log_likelihood(self,surrogate, theta):
        """
        Evaluates the log-likelihood given a dgala.
        """
        data = torch.cat([self.observation_locations, theta.repeat(self.observation_locations.size(0), 1)], dim=1).float()
        surg_mu, surg_sigma = surrogate(data)

        surg_mu = surg_mu[:,0].detach().view(-1, 1)
        surg_sigma = surg_sigma[:, 0].detach().view(-1, 1)

        sigma = self.observation_noise ** 2 + surg_sigma
        dy = surg_mu.shape[0]

        cte = 0.5 * (dy * torch.log(torch.tensor(2 * torch.pi)) + torch.sum(torch.log(sigma)))

        return -0.5 * torch.sum(((self.observations_values - surg_mu.reshape(-1, 1)) ** 2) / sigma)- cte
    
    def get_likelihood_function(self, surrogate, likelihood_methods):
        """Precompute and return the appropriate likelihood function for a given surrogate."""
        for surrogate_type, likelihood_func in likelihood_methods.items():
            if isinstance(surrogate, surrogate_type):
                return lambda theta: likelihood_func(surrogate, theta)
        raise ValueError(f"Surrogate of type {type(surrogate).__name__} is not supported.")

    def log_likelihood_outer(self, theta):
        return self.log_likelihood_outer_func(theta)

    def log_likelihood_inner(self, theta):
        return self.log_likelihood_inner_func(theta)
    