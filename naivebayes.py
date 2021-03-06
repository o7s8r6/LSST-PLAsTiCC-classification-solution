from __future__ import print_function, division
import numpy
from numpy import exp, pi, sqrt, log10, log
import matplotlib.pyplot as plt

import pypmc
from pypmc.density.mixture import create_gaussian_mixture, create_t_mixture
from pypmc.mix_adapt.variational import GaussianInference
from pypmc.sampler.importance_sampling import ImportanceSampler
from sklearn.base import BaseEstimator, ClassifierMixin
from sklearn.utils.validation import check_X_y, check_array, check_is_fitted
from sklearn.utils.multiclass import unique_labels
from sklearn.metrics import euclidean_distances
import scipy.stats

import joblib
from joblib import Parallel, delayed

def plothist(x, label):
	x = x[numpy.isfinite(x)]
	if x.max() > x.min():
		plt.hist(x, bins=1000, histtype='step', density=True, cumulative=True, label=label)
	else:
		plt.vlines(x, 0, 1, label=label)

def _make_single_mixture(x, Ngauss_init):
	# split CDF into segments. In each segment, put a gaussian
	# the benefit is that this supports multi-modality easily
	# and the parts should have equal weight already.
	q =  [i * 100.0 / Ngauss_init for i in range(1, Ngauss_init)]
	percentiles = numpy.percentile(x, [0] + q + [100]).astype(float)
	# avoid too small widths, which can occur with fake values
	mincov = ((percentiles[-1] - percentiles[0]) / 100.0)**2
	
	means = ((percentiles[1:] + percentiles[:-1]) / 2.).reshape((Ngauss_init, 1))
	covs  = ((percentiles[1:] - percentiles[:-1])**2).reshape((Ngauss_init, 1, 1))
	covs[~(covs > mincov)] = mincov
	return means, covs
	
	# a sequence of Ngauss_init next to each other
	lo, hi = x.min(), x.max()
	means = numpy.linspace(lo, hi, Ngauss_init).reshape((-1,1))
	covs = [numpy.array([[(hi - lo)**2]])] * Ngauss_init
	return means, covs

def _make_mixture_for_column(colname, col, verbose, Ngauss_init, nmin_multigauss, VB_iter):
	col = col[numpy.isfinite(col)]
	std = col.std()
	if len(col) == 0 or not(std > 0): # no useful samples
		if verbose > 0: print('    column %s: no data' % colname)
		return None
	elif len(col) < nmin_multigauss or VB_iter == 0:
		if verbose > 0: print('    column %s: %s +- %s' % (colname, col.mean(), std))
		return create_gaussian_mixture(numpy.array([[col.mean()]], dtype=float), [numpy.array([[std]], dtype=float)])
	else:
		means, covs = _make_single_mixture(col, Ngauss_init)
		mix = create_gaussian_mixture(means, covs)
		
		if verbose > 0: print('    column %s: running VB...' % colname)
		vb = GaussianInference(col.reshape(-1,1), 
			initial_guess=mix, W0=numpy.eye(1)*1e10)
		vb_prune = 0.5 * len(vb.data) / vb.K
		vb.run(VB_iter, rel_tol=1e-8, abs_tol=1e-5, 
			prune=vb_prune, verbose=verbose > 1)
		mix = vb.make_mixture()
		return mix


class MultiGaussNaiveBayesClassifier(BaseEstimator, ClassifierMixin):
	"""
	Flexible Naive Bayesian classifier which
	* Can handle nan values gracefully in training and testing
	* Can handle multiple classes
	* Can handle imbalanced classes
	* Uses a mixture of Gaussians to find each feature distribution
	* Can use Variational Bayes to find the optimum gaussian representation
	* Can use log-normal distributions: positive features are converted to log
	* Can produce diagnostic plots for each informative feature
	
	Letting Variational Bayes handle finding components has the benefit
	that it approximates the true distribution well, but does not overfit.
	
	"""
	def __init__(self, all_labels=None, nmin_multigauss=10, Ngauss_init=5, 
		VB_iter=1000, noteworthy_information = 100, 
		plot_prefix = None, column_names = None, verbose=0,
		parallel = None):
		"""
		all_labels: class labels to consider. 
			If None (default), taken from y in fit().
			Useful to set if you have more classes than are in 
			the training data.
		
		nmin_multigauss: 
			Minimum number of class training samples for attempting
			a gaussian mixture. If fewer, a single Gaussian is used.
		
		Ngauss_init:
			Starting guess for the number of gaussian components.
			These are reduced with Variational Bayes.
		
		VB_iter:
			Number of Variational Bayes iterations.
		
		noteworthy_information:
			The information gain of each feature (Kulback-Leibler divergence in nats) 
			is measured. Features powerful in distinguishing classes are highlighted.
			This parameter sets the threshold (in nats).
		
		plot_prefix:
			If set, create distribution plots for useful features
			showing the class distribution and the VB approximation.
		
		column_names:
			names of the features
		
		verbose:
			if 0: silent
			if 1: print usefulness information on each feature and ongoing computations
			if 2: like 1, but give also details on VB progress
		
		parallel:
			joblib.Parallel object for parallelisation
			If set to None (default), no parallelisation is performed
			
		
		column_names:
			
		
		"""
		self.all_labels = all_labels
		self.nmin_multigauss = nmin_multigauss
		self.Ngauss_init = Ngauss_init
		self.VB_iter = VB_iter
		self.minprob = 1e-5
		self.noteworthy_information = noteworthy_information
		self.plot_prefix = plot_prefix
		self.column_names = column_names
		self.islogvar = None
		self.verbose = verbose
		self.parallel = None
	
	def _make_mixture(self, x, colnames = None):
		if colnames is None:
			colnames = ['col%d' % (i+1) for i in range(x.shape[1])]
		assert len(colnames) == x.shape[1], (colnames, x.shape[1])
		
		if self.parallel is None:
			return [_make_mixture_for_column(colname, col, 
				verbose=self.verbose, Ngauss_init=self.Ngauss_init, 
				nmin_multigauss=self.nmin_multigauss, VB_iter=self.VB_iter) 
				for colname, col in zip(colnames, x.transpose())]
		else:
			return list(self.parallel(delayed(_make_mixture_for_column)(
				colname, col, verbose=self.verbose, Ngauss_init=self.Ngauss_init, 
				nmin_multigauss=self.nmin_multigauss, VB_iter=self.VB_iter)
				for colname, col in zip(colnames, x.transpose())))
	
	def _evaluate_feature_dist(self, feature_distributions, x, logprior):
		assert len(feature_distributions) == x.shape[1], (len(feature_distributions), x.shape)
		logprob = numpy.zeros((len(x), len(feature_distributions)))
		for i, mix in enumerate(feature_distributions):
			if mix is None:
				pass
			else:
				means = numpy.array([g.mu for g in mix.components])
				variances = numpy.array([g.sigma[0] for g in mix.components])
				weights = mix.weights.reshape(means.shape)
				
				logprob_col = - 0.5 * log(variances * 2 * pi) \
					- 0.5 * ((x[:,i].reshape((1,-1)) - means)**2 / variances) \
					+ log(weights)
				
				# sum across gaussian components:
				logprob_col = scipy.misc.logsumexp(logprob_col, axis=0)
				assert len(logprob_col) == len(x), (logprob_col.shape, x.shape)
				logprob[:,i] = logprob_col

		# handle missing values:
		logprob[~numpy.isfinite(logprob)] = 0
		# avoid overly small values:
		# In the tails of the distribution, we know we will not be accurate
		logprob[logprob < log(self.minprob)] = log(self.minprob)
		# multiply across columns:
		logprob = logprob.sum(axis=1)
		# combine with prior
		return logprob + logprior
			
	def _evaluate_information_single(self, columns, XT, l1, features1, mask1, l2, features2, mask2):
		lasti = None
		made_plot = False
		if columns is None:
			columns = ['col%d' % (i+1) for i in range(XT.shape[1])]
		
		KL_vector = []
		doplot = self.plot_prefix is not None
		Nsamples = 2000
		for i, (col, mix1, mix2) in enumerate(zip(columns, features1, features2)):
			if mix1 is None or mix2 is None: 
				# one of the two features does not have a distribution here
				continue

			means1 = numpy.array([g.mu[0] for g in mix1.components])
			stdevs1 = numpy.array([g.sigma[0][0]**0.5 for g in mix1.components])
			weights1 = mix1.weights
			means2 = numpy.array([g.mu for g in mix2.components])
			stdevs2 = numpy.array([g.sigma[0][0]**0.5 for g in mix2.components])
			weights2 = mix2.weights
			
			# here we measure how much information we gain from this feature
			sampler = ImportanceSampler(mix2.evaluate, mix1)
			sampler.run(Nsamples)
			KL = -log(sampler.weights[:]+1e-300).mean()
			KL_vector.append(KL)
			if self.verbose > 0:
				print('%3d %3d | %-20s%s | %6d nat   %s' % (l1, l2, col, 
					' log' if self.islogvar[0,i] else '    ', KL, 
					'***' if KL > self.noteworthy_information else ''))
			
			if KL > self.noteworthy_information and doplot:
				if lasti is not None :
					plt.plot(XT[mask1,i], XT[mask1,lasti], 's ', ms=2, label='class %d: %d' % (l1, mask1.sum()))
					plt.plot(XT[mask2,i], XT[mask2,lasti], 'o ', ms=2, label='class %d: %d' % (l2, mask2.sum()))
					mask = ~numpy.logical_or(mask1, mask2)
					plt.plot(XT[mask,i], XT[mask,lasti], 'x', ms=2, label='other', color='gray', alpha=0.5)
					plt.xlabel(columns[i])
					plt.ylabel(columns[lasti])
					if self.islogvar[0,i]:
						plt.xscale('log')
					if self.islogvar[0,lasti]:
						plt.yscale('log')
					plt.legend(loc='best')
					plt.savefig(self.plot_prefix + '%dvs%d_%s-%s.pdf' % (l1, l2, columns[i], columns[lasti]))
					plt.close()
					made_plot = True
				lasti = i
				
				plothist(XT[mask1,lasti], label='class %d: %d' % (l1, mask1.sum()))
				plothist(XT[mask2,lasti], label='class %d: %d' % (l2, mask2.sum()))
				mask = ~numpy.logical_or(mask1, mask2)
				plothist(XT[mask,lasti], label='other')
				goodx = XT[~mask,lasti]
				goodx = goodx[numpy.isfinite(goodx)]
				lo, hi = goodx.min(), goodx.max()
				samples = numpy.linspace(lo, hi, 400)
				y1 = numpy.sum([scipy.stats.norm(m, s).cdf(samples) * w for m, s, w in zip(means1, stdevs1, weights1)], axis=0)
				y2 = numpy.sum([scipy.stats.norm(m, s).cdf(samples) * w for m, s, w in zip(means2, stdevs2, weights2)], axis=0)
				assert len(y1) == len(samples)
				plt.plot(samples, y1, '--')
				plt.plot(samples, y2, ':')
				plt.xlabel(columns[lasti] + ": %s-%s" % (lo, hi))
				plt.legend(loc='best')
				plt.savefig(self.plot_prefix + '%dvs%d_%s.pdf' % (l1, l2, columns[lasti]))
				plt.close()
		return KL_vector
	
	def fit(self, X, y):
		X, y = check_X_y(X, y, force_all_finite='allow-nan')
		print(X.shape, y.shape)
		if self.all_labels is None:
			self.classes_ = unique_labels(y)
		else:
			self.classes_ = self.all_labels
		self.X_ = X
		self.y_ = y
		if self.islogvar is None:
			self.islogvar = ~(X <= 0).any(axis=0).reshape((1, -1))
		
		XT = numpy.where(self.islogvar, log10(X), X)
		
		class_properties = []
		j = []
		for i, label in enumerate(self.classes_):
			mask = y == label
			if not mask.any():
				continue
			
			if self.verbose > 0: 
				print("Analyzing class %s (%d members)... " % (label, mask.sum()))
			feature_distributions = self._make_mixture(XT[mask,:], self.column_names)
			class_properties.append((label, feature_distributions))
			
			j.append(i)
		self.populated_columns_ = numpy.array(j)
		
		KL = []
		for l1, feature_distributions in class_properties:
			KL_vector = []
			for l2, feature_distributions2 in class_properties:
				if l2 <= l1: continue
				KL12 = self._evaluate_information_single(self.column_names, XT, 
					l1, feature_distributions, y == l1, 
					l2, feature_distributions2, y == l2)
				KL_vector.append(KL12)
			KL.append(KL_vector)
		self.KL_ = numpy.array(KL)
		self.class_properties_ = class_properties
		return self
	
	def _predict(self, X):
		check_is_fitted(self, ['X_', 'y_'])
		X = check_array(X, force_all_finite='allow-nan')
		
		Ncovered = len(self.classes_)
		logprior = -log(Ncovered)
		
		XT = numpy.where(self.islogvar, log10(X), X)
		Xlogprobs = numpy.empty((len(X), len(self.class_properties_)))
		for i, (l1, feature_distributions) in enumerate(self.class_properties_):
			Xlogprobs[:,i] = self._evaluate_feature_dist(feature_distributions, XT, logprior)

		logprobany = scipy.misc.logsumexp(Xlogprobs, axis=1)
		nearest = Xlogprobs.argmax(axis=1)
		Xlogprobs -= logprobany.reshape((-1,1))
		self.nearest_ = nearest
		self.logproba_ = Xlogprobs
		self.surprise_ = logprobany

	def predict(self, X):
		self._predict(X)
		return self.nearest_
	
	def predict_proba(self, X):
		self._predict(X)
		return exp(self.logproba_)

	def predict_log_proba(self, X):
		self._predict(X)
		return self.logproba_

if __name__ == '__main__':
	from sklearn.utils.estimator_checks import check_estimator
	check_estimator(MultiGaussNaiveBayesClassifier)

