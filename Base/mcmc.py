import torch
import torch.distributions as dist
import torch.multiprocessing as mp

from pyro.distributions import Distribution

import numpy as np
from tqdm import tqdm  # For a progress bar



class MetropolisHastings(torch.nn.Module):
    """Implements Metropolis Hastings with multiple chains and configurable prior, likelihood, and proposal."""
    
    def __init__(self, observation_locations, observations_values, nparameters=2, 
                 observation_noise=0.5, nsamples=1000000, burnin=None,  
                 proposal_type="random_walk", step_size=0.1, device="cpu"):
        super(MetropolisHastings, self).__init__()

        # Likelihood data
        self.device = device
        self.observation_locations = torch.tensor(observation_locations, dtype=torch.float64, device=self.device)
        self.observations_values = torch.tensor(observations_values, dtype=torch.float64, device=self.device)
        self.observation_noise = observation_noise
        self.nparameters = nparameters
        
        # MCMC settings
        self.nsamples = nsamples
        self.burnin = int(self.nsamples * 0.1) if burnin is None else burnin
        self.proposal_type = proposal_type
        self.dt = step_size  # Step size for proposals

    def log_prior(self, theta):
        """Define the prior distribution (e.g., Gaussian, Uniform, etc.). Must be overridden."""
        raise NotImplementedError("log_prior must be implemented in a subclass.")

    def log_likelihood(self, theta):
        """Define the likelihood function. Must be overridden."""
        raise NotImplementedError("log_likelihood must be implemented in a subclass.")

    def proposal(self, theta, dt):
        """Proposal with independent step sizes for each chain."""
        if self.proposal_type == "random_walk":
            return theta + torch.normal(mean=torch.zeros_like(theta), std=dt).to(self.device)  # Scale noise by dt (per chain)
        elif self.proposal_type == "langevin":
            if not theta.requires_grad:
                theta.requires_grad_()  # Ensure theta is differentiable
            gradient = torch.autograd.grad(self.log_likelihood(theta), theta, retain_graph=True)[0]
            return theta + 0.5 * dt * gradient + dt * torch.randn_like(theta)


    def run_chain(self, verbose=True):
        """Run Metropolis-Hastings """
        theta = torch.empty((self.nparameters), device=self.device).uniform_(-1, 1)
        samples = torch.zeros((self.nsamples + self.burnin, self.nparameters), device=self.device)
        accepted_proposals = 0

        # Initialize separate dt for each chai
        dt =  self.dt
        
        if verbose:
            pbar = tqdm(range(self.nsamples + self.burnin), desc="Running MCMC", unit="step")
        else:
            pbar = range(self.nsamples + self.burnin)

        for i in pbar:
            theta_proposal = self.proposal(theta, dt)  # Pass dt to proposal

            log_posterior = self.log_prior(theta) + self.log_likelihood(theta)
            log_posterior_proposal = self.log_prior(theta_proposal) + self.log_likelihood(theta_proposal)

            # Compute acceptance probabilities (vectorized)
            a = torch.exp(log_posterior_proposal - log_posterior).clamp(max=1.0)

            if torch.rand(1, device=self.device) < a:
                theta = theta_proposal
                accepted_proposals += 1

            # Only store samples after burn-in
            samples[i, :] = theta

            # Adaptive step size adjustment (each chain updates its own dt)
            dt += dt * (a.item() - 0.234) / (i + 1)

            if verbose and (i % (self.nsamples // 10) == 0)and (i!=0):
                pbar.set_postfix(acceptance_rate=f"{accepted_proposals / (i+1):.4f}", proposal_variance=f"{dt:.4f}")

        return samples[self.burnin:,:].detach().cpu().numpy(),accepted_proposals/self.nsamples


    def _run_chain(self, seed, result_queue):
        """Runs a single chain with a given random seed (for parallel execution)."""
        torch.manual_seed(seed)
        samples, accepted_proposals = self.run_chain(verbose=False)
        result_queue.put((samples, accepted_proposals))

    def run_chains(self,nchains = 2):
        """Run multiple chains in parallel or sequentially."""
        seeds = [torch.randint(0, 100000, (1,)).item() for _ in range(nchains)]
        processes = []
        result_queue = mp.Queue()

        for i in range(nchains):
            p = mp.Process(target=self._run_chain, args=(seeds[i], result_queue))
            processes.append(p)
            p.start()

        for p in processes:
            p.join()

        results = []
        while not result_queue.empty():
            results.append(result_queue.get())

        return results



class MCMCDA(torch.nn.Module):
    """
    Delayed Acceptance for 
    """
    def __init__(self, observation_locations, observations_values, nparameters=2, 
                 observation_noise=0.5, iter_mcmc=1000000, iter_da = 20000,                 
                 proposal_type="random_walk", step_size=0.1, device="cpu"):
        super(MCMCDA, self).__init__()

        # Likelihood data
        self.device = device
        self.observation_locations = torch.tensor(observation_locations, dtype=torch.float64, device=self.device)
        self.observations_values = torch.tensor(observations_values, dtype=torch.float64, device=self.device)
        self.observation_noise = observation_noise
        self.nparameters = nparameters
        
        # MCMC settings
        self.iter_da = iter_da
        self.iter_mcmc = iter_mcmc
        self.proposal_type = proposal_type
        self.dt = step_size  # Step size for proposals
        self.to(device)

    def log_prior(self, theta):
        """Define the prior distribution (e.g., Gaussian, Uniform, etc.). Must be overridden."""
        raise NotImplementedError("log_prior must be implemented in a subclass.")

    def log_likelihood_outer(self, theta):
        """Define the likelihood function for the coarse model. Must be overridden."""
        raise NotImplementedError("log_likelihood must be implemented in a subclass.")
    
    def log_likelihood_inner(self, theta):
        """Define the likelihood function for the finner model. Must be overridden."""
        raise NotImplementedError("log_likelihood must be implemented in a subclass.")

    def proposal(self, theta, dt):
        """Proposal with independent step sizes for each chain."""
        if self.proposal_type == "random_walk":
            return theta + torch.normal(mean=torch.zeros_like(theta), std=dt).to(self.device)  # Scale noise by dt (per chain)
        elif self.proposal_type == "langevin":
            if not theta.requires_grad:
                theta.requires_grad_()  # Ensure theta is differentiable
            gradient = torch.autograd.grad(self.log_likelihood(theta), theta, retain_graph=True)[0]
            return theta + 0.5 * dt * gradient + dt * torch.randn_like(theta)


    def run_chain(self, samples = False, verbose=True):
        """Run Metropolis-Hastings """
        theta = torch.empty((self.nparameters), device=self.device).uniform_(-1, 1)
        acceptance_list = torch.zeros((self.iter_da), device=self.device)
        samples_outer = torch.zeros((self.iter_mcmc, self.nparameters), device=self.device)
        samples_inner = []
        # Initialize separate dt for each chai
        dt =  self.dt

        outer_mh = 0
        if verbose:
            pbar = tqdm(range(self.iter_mcmc), desc="Running MCMC", unit="step")
        else:
            pbar = range(self.iter_mcmc)

        for i in pbar:
            # Propose new theta values
            theta_proposal = self.proposal(theta,dt)
            
            # Compute the current log-posterior
            log_posterior_outer = self.log_prior(theta) + self.log_likelihood_outer(theta)
            log_posterior_proposal_outer = self.log_prior(theta_proposal) + self.log_likelihood_outer(theta_proposal)

            # Compute the acceptance ratio
            a = torch.exp(log_posterior_proposal_outer - log_posterior_outer).clamp(max=1.0)

            if torch.rand(1, device=self.device) < a:
                theta = theta_proposal.clone()
                outer_mh += 1

            if samples:
                samples_outer[i,:] = theta

            # Adaptive step size adjustment 
            dt += dt * (a.item() - 0.234) / (i + 1)

            if verbose and i % (self.iter_mcmc // 10) == 0 and (i!=0):
                pbar.set_postfix(acceptance_rate=f"{outer_mh / (i+1):.4f}", proposal_variance=f"{dt:.4f}")
            

        print("Starting Delayed Acceptance....")

        if verbose:
            pbar = tqdm(range(self.iter_da), desc="Running Delayed Acceptance", unit="step")
    
        inner_accepted,inner_mh = 0,0
        while inner_mh < self.iter_da:
            # Propose new theta values
            theta_proposal = self.proposal(theta,dt)
            
            # Compute the current log-posterior
            log_posterior_outer = self.log_prior(theta) + self.log_likelihood_outer(theta)
            log_posterior_proposal_outer = self.log_prior(theta_proposal) + self.log_likelihood_outer(theta_proposal)

            # Compute the acceptance ratio
            a = torch.clamp(torch.exp(log_posterior_proposal_outer - log_posterior_outer), max=1.0)

            # Accept or reject the proposal
            if torch.rand(1, device=self.device) < a:
                inner_mh += 1
                log_posterior_inner = self.log_prior(theta) + self.log_likelihood_inner(theta)
                log_posterior_proposal_inner = self.log_prior(theta_proposal) + self.log_likelihood_inner(theta_proposal)

                # Compute the acceptance ratio
                a = torch.clamp(torch.exp(log_posterior_proposal_inner - (log_posterior_inner))*(1/a), max=1.0)

                if verbose:
                    pbar.update(1)

                if torch.rand(1, device=self.device) < a:
                    theta = theta_proposal.clone()
                    inner_accepted += 1
                    acceptance_list[inner_mh-1] +=1

            if samples:
                # Store the current sample and step size
                samples_inner.append(theta)

                # Update progress bar and print progress every 10% (or any interval)
            if verbose and (inner_mh % (self.iter_da // 10) == 0) and (inner_mh != 0):
                acceptance_rate = inner_accepted / inner_mh
                pbar.set_postfix(acceptance_rate=f"{acceptance_rate:.4f}")
            
        # End progress bar
        if verbose:
            pbar.close()

        if verbose:
            print(f"Times inner step {inner_mh:.4f}, Acceptance Rate: {inner_accepted / inner_mh:.4f}")
        if samples:
            return torch.tensor(samples_inner),samples_outer
        else:
            return acceptance_list
    

            







# class MetropolisHastings:
#     """
#     A class to perform Metropolis-Hastings sampling for parameter inference.
#     It can use a neural network surrogate model or a numerical solver for likelihood evaluation.
#     """
#     def __init__(self, x, y, surrogate=None, nparam=2, sig=1.0, dt_init=0.5, 
#                  mean=True, numerical=False,vert=30,lam = 1 /4, M = 2, reg=1e-3, device='cpu'):
#         """
#         Initialize the Metropolis-Hastings sampler.
#         """
#         self.device = device

#         self.surrogate = surrogate
#         self.x = torch.tensor(x, dtype=torch.float32, device=self.device)
#         self.y = torch.tensor(y, dtype=torch.float32, device=self.device)
#         self.sig = torch.tensor(sig, dtype=torch.float32, device=self.device)
#         self.dt = torch.tensor(dt_init, dtype=torch.float32, device=self.device)
#         self.reg = torch.tensor(reg,dtype=torch.float32, device=self.device)  # Regularization parameter
#         self.lam = lam
#         self.M = M

#         self.nparam = nparam
#         self.mean = mean
#         self.numerical = numerical
#         self.vert = vert

#         # Initialize the FEMSolver once, if numerical solver is used
#         if self.numerical:
#             self.solver = FEMSolver(np.zeros(self.nparam), self.lam,self.M, vert=self.vert)

#         self.scaling_term = 1 / (2 + torch.sqrt(2 * torch.pi * self.reg)).to(self.device)  # Calculate once


#     def log_prior_alpha(self, theta):
#         """
#         Log prior with Moreau-Yosida regularization for the uniform distribution between -1 and 1.
#         """
#         # Regularization term for all theta values
#         regularization_term = -(torch.clamp(torch.abs(theta) - 1, min=0) ** 2) / (2 * self.reg)
#         return torch.sum(regularization_term + torch.log(self.scaling_term))

#     def proposals(self, alpha):
#         """
#         Generates proposals for the next step in the Metropolis-Hastings algorithm using a normal distribution.
        
#         Args:
#             alpha: Current parameter values.
        
#         Returns:
#             New proposed parameter values.
#         """
#         # Generate normal proposals
#         proposal = alpha + torch.normal(mean=torch.zeros_like(alpha), std=self.dt).to(self.device)
        
#         return proposal

#     def log_likelihood(self, pr):
#         """
#         Evaluates the log-likelihood given the surrogate model or numerical solver.
        
#         Args:
#             pr: Current parameters.
        
#         Returns:
#             Log-likelihood value.
#         """
#         if self.numerical:
#             self.solver.theta = pr.cpu().numpy()  # Convert to numpy for FEM solver
#             self.solver.solve()
#             surg = self.solver.eval_at_points(self.x.cpu().numpy()).reshape(-1, 1)

#             surg = torch.tensor(surg, device=self.device)
#             return -0.5 * torch.sum(((self.y - surg) ** 2) / (self.sig ** 2))

#         elif self.mean:
#             data = torch.cat([self.x, pr.repeat(self.x.size(0), 1)], dim=1)
#             surg = self.surrogate(data).detach().reshape(-1, 1)
#             return -0.5 * torch.sum(((self.y - surg) ** 2) / (self.sig ** 2))

#         else:
#             data = torch.cat([self.x, pr.repeat(self.x.size(0), 1)], dim=1)
#             surg_mu, surg_sigma = self.surrogate(data)

#             surg_mu = surg_mu.view(-1, 1)
#             surg_sigma = surg_sigma[:, :, 0].view(-1, 1)

#             sigma = self.sig ** 2 + surg_sigma
#             dy = surg_mu.shape[0]

#             cte = 0.5 * (dy * torch.log(torch.tensor(2 * torch.pi)) + torch.sum(torch.log(sigma)))

#             return -0.5 * torch.sum(((self.y - surg_mu.reshape(-1, 1)) ** 2) / sigma)- cte

        
#     def log_posterior(self, pr):
#         """
#         Evaluates the log-posterior using the surrogate model.
        
#         Args:
#             pr: Current parameters.
        
#         Returns:
#             Log-posterior value.
#         """
#         return self.log_likelihood(pr) + self.log_prior_alpha(pr)

#     def run_sampler(self, n_chains, verbose=True):
#         """
#         Run the Metropolis-Hastings sampling process sequentially.
        
#         Args:
#             n_chains: Number of steps in the chain.
#             verbose: Whether to print progress (default True).
        
#         Returns:
#             alpha_samp: Sampled parameter values.
#             dt_tracker: Step size progression over the chain.
#         """
#         # Initialize the parameters randomly within the prior range
#         alpha = torch.empty(self.nparam, device=self.device).uniform_(-1, 1)
#         alpha_samp = torch.zeros((n_chains, self.nparam), device=self.device)
#         dt_tracker = torch.zeros(n_chains, device=self.device)
#         acceptance_rate = 0

#         for i in range(n_chains):
#             # Propose new alpha values
#             alpha_proposal = self.proposals(alpha)
            
#             # Compute the current log-posterior
#             log_posterior_current = self.log_posterior(alpha)
#             log_posterior_proposal = self.log_posterior(alpha_proposal)

#             # Compute the acceptance ratio
#             a = torch.clamp(torch.exp(log_posterior_proposal - log_posterior_current), max=1.0)
            
#             # Accept or reject the proposal
#             if torch.rand(1, device=self.device) < a:
#                 alpha = alpha_proposal
#                 acceptance_rate += 1

#             # Store the current sample and step size
#             alpha_samp[i] = alpha
#             dt_tracker[i] = self.dt

#             # Adaptive step size adjustment 
#             self.dt += self.dt * (a - 0.234) / (i + 1)

#             del log_posterior_current, log_posterior_proposal, alpha_proposal
#             if self.device != "cpu":
#                 torch.cuda.empty_cache()

#             # Print progress every 10% of the steps
#             if verbose and i % (n_chains // 10) == 0:
#                 print(f"Iteration {i}, Acceptance Rate: {acceptance_rate / (i + 1):.3f}, Step Size: {self.dt:.4f}")

#         if verbose:
#             print(f"Final Acceptance Rate: {acceptance_rate / n_chains:.3f}")

#         return alpha_samp.detach().cpu().numpy(), dt_tracker.detach().cpu().numpy()





class MoreauYosidaPrior(Distribution):
    def __init__(self, lam, batch_shape=torch.Size([]), device='cpu'):
        super().__init__()
        self.device = device
        self.batch_shape = torch.Size(batch_shape)  # Ensure batch_shape is a torch.Size object
        self.lam = torch.tensor(lam, dtype=torch.float32, device=self.device)


    @property
    def event_shape(self):
        """The event shape for this distribution is 1-dimensional."""
        return torch.Size([1])

    @property
    def support(self):
        """The support of the distribution is the whole real line."""
        return dist.constraints.real

    def log_prob(self, x):
        """Calculate the log probability of x under the Moreau-Yosida prior."""
        if not isinstance(x, torch.Tensor):
            x = torch.tensor(x, dtype=torch.float32, device=self.device)

        # Calculate the regularization term
        regularization_term = -(torch.clamp(torch.abs(x) - 1, min=0) ** 2) / (2 * self.lam)
        log_prob = regularization_term - torch.log(2 + torch.sqrt(2 * torch.pi * self.lam))

        return log_prob

    def sample(self, sample_shape=torch.Size()):
        """Sample from the Moreau-Yosida prior using importance sampling."""
        sample_shape = torch.Size(sample_shape)  # Ensure sample_shape is a torch.Size object
        total_shape = self.batch_shape + sample_shape  # Concatenate batch_shape with sample_shape

        # Proposal distribution with heavier tails (e.g., Normal distribution)
        proposal_dist = dist.Normal(loc=0.0, scale=2.0)  # Wider scale for more tail coverage
        proposals = proposal_dist.sample(total_shape).to(self.device)

        # Calculate log probabilities for the proposals under the Moreau-Yosida prior
        log_prob_samples = self.log_prob(proposals)

        # Calculate log probability of the proposal distribution
        log_prob_proposal = proposal_dist.log_prob(proposals)

        # Calculate weights for importance sampling
        weights = torch.exp(log_prob_samples - log_prob_proposal)

        # Clamp weights to avoid negative entries
        weights = torch.clamp(weights, min=0)

        # Normalize weights
        weights_sum = torch.sum(weights)
        if weights_sum > 0:  # Prevent division by zero
            weights /= weights_sum
        else:
            # If all weights are zero, return uniformly sampled values
            return proposals

        # Resample according to weights (multinomial resampling)
        idx = torch.multinomial(weights, total_shape[0], replacement=True)
        resampled_proposals = proposals[idx]

        return resampled_proposals

    def expand(self, batch_shape, _instance=None):
        """Expand the distribution to a new batch shape."""
        return MoreauYosidaPrior(self.lam, batch_shape, self.device)



class DAHM:
    """
    A class to perform Metropolis-Hastings sampling for parameter inference.
    It can use a neural network surrogate model or a numerical solver for likelihood evaluation.
    """

    def __init__(self, x, y, vert1=2, vert2 = 100, nparam=2, sig=1.0, dt_init=0.5, 
                  lam = 1 /4, M = 2, reg=1e-3, device='cpu'):
        """
        Initialize the Metropolis-Hastings sampler.
        
        Args:
            surrogate: Surrogate neural network model.
            x: Input data (independent variables).
            y: Observed data (dependent variables).
            sig: Standard deviation of the noise in the observations (default 1.0).
            dt_init: Initial step size for the proposal distribution (default 0.5).
            numerical: Whether to use a numerical solver instead of the surrogate model (default False).
            roots: Roots for FEM solver, used if numerical=True (optional).
            vert: Vertices for FEM solver, used if numerical=True (optional).
            lam: Regularization parameter for the Moreau-Yosida regularization.
            device: Device to run the model (default 'cpu').
        """
        self.device = device

        self.x = torch.tensor(x, dtype=torch.float32, device=self.device)
        self.y = torch.tensor(y, dtype=torch.float32, device=self.device)
        self.sig = torch.tensor(sig, dtype=torch.float32, device=self.device)
        self.dt = torch.tensor(dt_init, dtype=torch.float32, device=self.device)
        self.reg = torch.tensor(reg,dtype=torch.float32, device=self.device)  # Regularization parameter
        self.lam = lam
        self.M = M

        self.nparam = nparam
        self.vert1 = vert1
        self.vert2 = vert2


        # Initialize the FEMSolver once, if numerical solver is used
        self.solver1 = FEMSolver(np.zeros(self.nparam), self.lam,self.M, vert=self.vert1)
        self.solver2 = FEMSolver(np.zeros(self.nparam), self.lam,self.M, vert=self.vert2)


        self.scaling_term = 1 / (2 + torch.sqrt(2 * torch.pi * self.reg)).to(self.device)  # Calculate once


    def log_prior_alpha(self, theta):
        """
        Log prior with Moreau-Yosida regularization for the uniform distribution between -1 and 1.
        """
        # Regularization term for all theta values
        regularization_term = -(torch.clamp(torch.abs(theta) - 1, min=0) ** 2) / (2 * self.reg)
        return torch.sum(regularization_term + torch.log(self.scaling_term))

    def proposals(self, alpha):
        """
        Generates proposals for the next step in the Metropolis-Hastings algorithm using a normal distribution.
        Args:
            alpha: Current parameter values.
        
        Returns:
            New proposed parameter values.
        """
        # Generate normal proposals
        proposal = alpha + torch.normal(mean=torch.zeros_like(alpha), std=self.dt).to(self.device)
        
        return proposal

    def log_likelihood(self,solver,pr):

        """
        Evaluates the log-likelihood given the surrogate model or numerical solver.
        
        Args:
            pr: Current parameters.
        
        Returns:
            Log-likelihood value.
        """
        solver.theta = pr.cpu().numpy()  # Convert to numpy for FEM solver
        solver.solve()
        surg = solver.eval_at_points(self.x.cpu().numpy()).reshape(-1, 1)

        surg = torch.tensor(surg, device=self.device)

        return -0.5 * torch.sum(((self.y - surg) ** 2) / (self.sig ** 2))

        
    def log_posterior(self, solver,pr):
        """
        Evaluates the log-posterior using the surrogate model.
        
        Args:
            pr: Current parameters.
        
        Returns:
            Log-posterior value.
        """
        return self.log_likelihood(solver,pr) + self.log_prior_alpha(pr)

    def run_sampler(self, n_chains, verbose=True):
        """
        Run the Metropolis-Hastings sampling process sequentially.
        
        Args:
            n_chains: Number of steps in the chain.
            verbose: Whether to print progress (default True).
        
        Returns:
            alpha_samp: Sampled parameter values.
            dt_tracker: Step size progression over the chain.
        """
        # Initialize the parameters randomly within the prior range
        alpha = torch.empty(self.nparam, device=self.device).uniform_(-1, 1)
        alpha_samp = torch.zeros((n_chains, self.nparam), device=self.device)
        #dt_tracker = torch.zeros(n_chains, device=self.device)
        acceptance_rate = 0
        inner_mh = 0

        for i in range(n_chains):
            # Propose new alpha values
            alpha_proposal = self.proposals(alpha)
            
            # Compute the current log-posterior
            log_posterior_current = self.log_posterior(self.solver1,alpha)
            log_posterior_proposal = self.log_posterior(self.solver1,alpha_proposal)

            # Compute the acceptance ratio
            a = torch.clamp(torch.exp(log_posterior_proposal - log_posterior_current), max=1.0)

            # Accept or reject the proposal
            if torch.rand(1, device=self.device) < a:
                inner_mh += 1
                log_posterior_current2 = self.log_posterior(self.solver2,alpha)
                log_posterior_proposal2 = self.log_posterior(self.solver2,alpha_proposal)

                # Compute the acceptance ratio
                a = torch.clamp(torch.exp(log_posterior_proposal2 - (log_posterior_current2))*(1/a), max=1.0)

                if torch.rand(1, device=self.device) < a:
                    alpha = alpha_proposal
                    acceptance_rate += 1

            # Store the current sample and step size
            alpha_samp[i] = alpha
            #dt_tracker[i] = self.dt

            # Adaptive step size adjustment 
            #self.dt += self.dt * (a - 0.234) / (i + 1)

            del log_posterior_current, log_posterior_proposal, alpha_proposal
            if self.device != "cpu":
                torch.cuda.empty_cache()

            # Print progress every 10% of the steps
            if verbose and i % (n_chains // 10) == 0 and (i!=0):
                print(f"Iteration {i}, Acceptance Rate: {acceptance_rate / inner_mh:.3f}, Step Size: {self.dt:.4f}")
                #print(f"Iteration {i}, Acceptance Rate: {acceptance_rate / (i + 1):.3f}, Step Size: {self.dt:.4f}")

        # if verbose:
        #     print(f"Final Acceptance Rate: {acceptance_rate / n_chains:.3f}")
            
        return alpha_samp.detach().cpu().numpy()
        #return alpha_samp.detach().cpu().numpy(), dt_tracker.detach().cpu().numpy()
