#!/usr/bin/env python3

from re import A
import numpy as np

from sklearn.base import BaseEstimator
from scipy.linalg import solve_triangular
from scipy.special import logsumexp
import warnings
from sklearn.exceptions import ConvergenceWarning
from sklearn.cluster import KMeans


class GaussianMixture(BaseEstimator):
    """Gaussian Mixture model solved using the Expectation maximization 
    algorithm.

    Parameters
    ----------
    n_components : int, default=1
        The number of mixture components.

    tol : float, default=1e-3
        The convergence threshold. EM iterations will stop when the
        lower bound average gain is below this threshold.

    reg_covar : float, default=1e-6
        Non-negative regularization added to the diagonal of covariance.
        Allows to assure that the covariance matrices are all positive.

    max_iter : int, default=100
        The number of EM iterations to perform.

    init_params : {'kmeans', 'random', array-like}, default='kmeans'
        The method used to initialize the responsabilities.
        Must be one of::
            'kmeans' : responsibilities are initialized using kmeans.
            'random' : responsibilities are initialized randomly.
            array-like with shape (n_samples, n_components)

    random_state : int, RandomState instance or None, default=None
        Controls the random seed given to the KMeans method chosen to initialize
        the parameters (see `init_params`).
        Pass an int for reproducible output across multiple function calls.

    Attributes
    ----------
    weights_ : array-like of shape (n_components,)
        The weights of each mixture components.

    means_ : array-like of shape (n_components, n_features)
        The mean of each mixture component.

    covariances_ : array-like
        The covariance of each mixture component, with shape (n_components,
        n_features, n_features)

    converged_ : bool
        True when convergence was reached in fit(), False otherwise.

    lower_bound_ : float
        Lower bound value on the log-likelihood (of the training data with
        respect to the model) of the best fit of EM.

    """
    def __init__(
        self,
        n_components=1,
        tol=1e-3,
        reg_covar=1e-6,
        max_iter=100,
        init_params="kmeans",
        random_state=None,
    ):

        self.n_components = n_components
        self.tol = tol
        self.reg_covar = reg_covar
        self.max_iter = max_iter
        self.init_params = init_params
        self.random_state = random_state

    def _initialize(self, X):
        """Initialization of the Gaussian mixture parameters.
        Parameters
        ----------
        X : array-like of shape (n_samples, n_features)
        """
        # Model is initialized but needs to be fitted
        self.fitted = False
        n_samples, _ = X.shape

        # Initialize samples responsabilities, i.e. probability of each sample
        # to belong to each component.
        # Assign cluster using kmeans prediction
        if self.init_params == "kmeans":
            resp = np.zeros((n_samples, self.n_components))
            labels = KMeans(n_clusters=self.n_components, n_init=1,
                                   random_state=self.random_state).fit(X).labels_
            resp[np.arange(n_samples), labels] = 1
        # Random initialization
        elif self.init_params == "random":
            # Random initialization
            resp = np.random.default_rng(self.random_state).random((n_samples, self.n_components))
            # Responsabilities are probabilities, i.e, must be normalized.
            resp /= resp.sum(axis=1)[:, np.newaxis]
        # Use init_params as array of responsabilities
        elif isinstance(self.init_params, np.ndarray) and \
                self.init_params.shape == (n_samples, self.n_components):
            resp = self.init_params
        else: 
            raise ValueError("Unimplemented initialization method '%s'"
                % self.init_params) 

        # Compute weights, means and covariance matrixes using the initialized
        # responsabilities
        self.weights_, self.means_, self.covariances_ = \
            self._estimate_gaussian_parameters(X, resp)
    
        # Pre-compute the cholesky decomposition of the covariance matrixes.
        self._compute_precision_cholesky()
            
            
    def _estimate_gaussian_parameters(self, X, resp):
        """Estimate the Gaussian mixture distribution parameters, that is, 
        mixture weights, means and covariance matrixes.
        
        Parameters
        ----------
        X : array-like of shape (n_samples, n_features)
            The input data array.
        resp : array-like of shape (n_samples, n_components)
            The responsibilities for each data sample in X.

        Returns
        -------
        weights : array-like of shape (n_components,)
            Probability of each component.
        means : array-like of shape (n_components, n_features)
            The centers of the current components.
        covariances : array-like
            The covariance matrix of the current components.
            The shape depends of the covariance_type.
        """
        _, n_components = resp.shape
        n_samples, n_features = X.shape
        
        # Compute un-normalize probability of each component. Represents
        # the probabilistic amount of samples in each component.
        weights = resp.sum(axis=0)

        # Compute new means
        means = np.dot(resp.T, X) / weights[:, np.newaxis]

        covariances = np.empty((n_components, n_features, n_features))
        for k in range(n_components):
            mean = X - means[k]
            # Compute covariances
            covariances[k] = np.dot(resp[:, k] * mean.T, mean) / weights[k]
            # Add regularization term to avoid numerical issues
            covariances[k].flat[:: n_features + 1] += self.reg_covar

        return weights/n_samples, means, covariances
    
    def _compute_precision_cholesky(self):
        """
        Compute the Cholesky decomposition of the precision matrixes.
        """
        n_components, n_features, _ = self.covariances_.shape
        self.precisions_cholesky_ = np.empty((n_components, n_features, n_features))
        for k, covariance in enumerate(self.covariances_):
            try:
                cov_chol = np.linalg.cholesky(covariance)
            except np.linalg.LinAlgError:
                raise ValueError( "Fitting the mixture model failed because"
                                 "some components have ill-defined empirical"
                                 "covariance (for instance caused by singleton"
                                 " or collapsed samples). Try to decrease the "
                                 "number of components, or increase reg_covar.")
    
            self.precisions_cholesky_[k] = solve_triangular(cov_chol,
                                                         np.eye(n_features),
                                                         lower=True).T 
    
    def _estimate_log_gaussian_prob(self, X):
        """Estimate the log Gaussian probability.
        ```
         \log P(X | Z ) = -\frac{k}{2}\log (2\pi) - \frac{k}{2}\log det(\Sigma) 
         - \frac{1}{2}(x- \mu)^T \Sigma^{-1}(x-\mu)
        ```
        
        Parameters
        ----------
        X : array-like of shape (n_samples, n_features)

        Returns
        -------
        log_prob : array, shape (n_samples, n_components)
        """
        n_samples, n_features = X.shape
        n_components, _ = self.means_.shape
        
        # Logarithm of determinant of precision matrix.
        log_det = np.log(np.linalg.det(self.precisions_cholesky_)) 

        # Compute quadratic form
        log_prob = np.empty((n_samples, n_components))
        for k, (mu, prec_chol) in enumerate(zip(self.means_, self.precisions_cholesky_)):
            y = np.dot(X, prec_chol) - np.dot(mu, prec_chol)
            log_prob[:, k] = np.sum(np.square(y), axis=1)
        
        return -.5 * (n_features * np.log(2 * np.pi) + log_prob) + log_det
    
    def _estimate_weighted_log_prob(self, X):
        """ Estimate the weighted log probabilities for each sample.
            ```
                \log (\pi_k \mathcal{N}(x_k: \mu_k, \Sigma_k))
            ```
        Parameters
        ----------
        X : array-like of shape (n_samples, n_features)
        Returns
        -------
        weighted_log_prob : array, shape (n_samples,)
            log p(X | Z) + log weights
        """ 
        return self._estimate_log_gaussian_prob(X) + np.log(self.weights_)
        

    def _estimate_log_prob_resp(self, X):
        """Estimate log probabilities and responsibilities for each sample.
        
        Parameters
        ----------
        X : array-like of shape (n_samples, n_features)
        Returns
        -------
        log_prob_norm : array, shape (n_samples,)
            log p(X)
        log_responsibilities : array, shape (n_samples, n_components)
            logarithm of the responsibilities
        """
        
        weighted_log_prob = self._estimate_weighted_log_prob(X)
        log_prob_norm = logsumexp(weighted_log_prob, axis=1)
        log_resp = weighted_log_prob - log_prob_norm[:, np.newaxis]
        return log_prob_norm, log_resp

    def _e_step(self, X):
        """E step.
        Parameters
        ----------
        X : array-like of shape (n_samples, n_features)
        Returns
        -------
        log_prob_norm : float
            Mean of the logarithms of the probabilities of each sample in X
        log_responsibility : array, shape (n_samples, n_components)
            Logarithm of the posterior probabilities (or responsibilities) of
            the point of each sample in X.
        """
        log_prob_norm, log_resp = self._estimate_log_prob_resp(X)
        return np.mean(log_prob_norm), log_resp

    def _m_step(self, X, log_resp):
        """M step.

        Parameters
        ----------
        X : array-like of shape (n_samples, n_features)
        log_resp : array-like of shape (n_samples, n_components)
            Logarithm of the posterior probabilities (or responsibilities) of
            the point of each sample in X.
        """
       
        self.weights_, self.means_, self.covariances_ = self._estimate_gaussian_parameters(
            X, np.exp(log_resp)
        )

        self._compute_precision_cholesky()

    def fit(self, X, y=None):
        """Estimate model parameters with the EM algorithm.
        The method iterates between E-step and M-step for ``max_iter``
        times until the change of likelihood or lower bound is less than
        ``tol``, otherwise, a ``ConvergenceWarning`` is raised.

        Parameters
        ----------
        X : array-like of shape (n_samples, n_features)
            List of n_features-dimensional data points. Each row
            corresponds to a single data point.
        Returns
        -------
        self
        """
        # Initialize model and lower bound
        self._initialize(X)
        self.converged_ = False
        prev_lower_bound = -np.infty

        for _ in range(self.max_iter):
            # E-step
            lower_bound, log_resp = self._e_step(X)
            # M-step
            self._m_step(X, log_resp)

            # Compute ELBO change
            change = lower_bound - prev_lower_bound
            prev_lower_bound = lower_bound
            
            # Check convergence criteria
            if abs(change) < self.tol:
                self.converged_ = True
                break

        if not self.converged_:
            warnings.warn(
                "Method did not converge.",  ConvergenceWarning,
            )

        self.lower_bound_ = lower_bound
        self.fitted = True
        return self

    def fit_predict(self, X, y=None):
        """Estimate model parameters with the EM algorithm.
        The method iterates between E-step and M-step for ``max_iter``
        times until the change of likelihood or lower bound is less than
        ``tol``, otherwise, a ``ConvergenceWarning`` is raised.
        Predict the labels for the data samples in X using trained model.

        Parameters
        ----------
        X : array-like of shape (n_samples, n_features)
            List of n_features-dimensional data points. Each row
            corresponds to a single data point.
        Returns
        -------
        labels : array, shape (n_samples,)
            Component labels.
        """

        return self.fit(X).predict(X)

    def predict(self, X):
        """Predict the labels for the data samples in X using trained model.
        Parameters
        ----------
        X : array-like of shape (n_samples, n_features)
            List of n_features-dimensional data points. Each row
            corresponds to a single data point.
        Returns
        -------
        labels : array, shape (n_samples,)
            Component labels.
        """
        if not self.fitted:
            raise("Model must be fitted before predicting any values.")
        return self._estimate_weighted_log_prob(X).argmax(axis=1)
