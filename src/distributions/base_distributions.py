from typing import Dict, Callable, Tuple, Union

import numpy as np
import pandas as pd
from scipy import stats, optimize, special


class NonConvergenceError(Exception):
    """ Raised when the optimization fails to converge """
    def __init__(self, message: str, dist: str) -> None:
        super().__init__(message)
        self.dist = dist


class MissingDataError(Exception):
    pass


def _get_optimization_result(data: pd.DataFrame, func: Callable,
                             initial_func: Callable) -> Tuple:
    """Finds the shape parameters of distributions which generates mean/sd close to actual mean/sd.

    Parameters
    ---------
    data :
        Table where each row has a `mean` and `standard_deviation` for a single distribution.
    func:
        The optimization objective function.  Takes arguments `initial guess`, `mean`, and `standard_deviation`.
    initial_func:
        Function to produce initial guess from a `mean` and `standard_deviation`.

    Returns
    --------
        A tuple of the optimization results.
    """

    mean, sd = data['mean'].values, data['standard_deviation'].values
    results = []
    with np.errstate(all='warn'):
        for i in range(len(mean)):
            initial_guess = initial_func(mean[i], sd[i])
            result = optimize.minimize(func, initial_guess, (mean[i], sd[i],), method='Nelder-Mead',
                                       options={'maxiter': 10000})
            results.append(result)
    return tuple(results)


def validate_parameters(*args, mean, std_dev):
    # TODO: how to check if they've actually passed mean/sd
    if len(args) != 0 and (mean or std_dev):
        raise ValueError("You may supply either a dataframe or mean and standard deviation but not both.")

    # FIXME: what if user does something like Dist(mean, sd)??



class BaseDistribution:
    """Generic vectorized wrapper around scipy distributions."""

    distribution = None

    def __init__(self, *args, mean: Union[pd.Series, float, int]=0, std_dev: Union[pd.Series, float, int]=0):

        # TODO: validate parameters

        if len(args) == 1:
            data = args[0]
        else:
            data = pd.DataFrame({'mean': mean, 'standard_deviation': std_dev})

        self._range = self._get_min_max(data)
        self._parameter_data = self._get_params(data)


    @staticmethod
    def _get_min_max(data: pd.DataFrame) -> Dict[str, np.ndarray]:
        """Gets the upper and lower bounds of the distribution support."""
        data_mean, data_sd = data['mean'], data['standard_deviation']
        alpha = 1 + data_sd ** 2 / data_mean ** 2
        scale = data_mean / np.sqrt(alpha)
        s = np.sqrt(np.log(alpha))
        x_min = stats.lognorm(s=s, scale=scale).ppf(.001)
        x_max = stats.lognorm(s=s, scale=scale).ppf(.999)
        return {'x_min': x_min, 'x_max': x_max}

    def _get_params(self, data: pd.DataFrame) -> Dict[str, pd.DataFrame]:
        raise NotImplementedError()

    def process(self, data: Union[np.ndarray, pd.Series], process_type: str,
                ranges: Dict[str, np.ndarray]) -> Union[np.ndarray, pd.Series]:
        """Function called before and after distribution looks to handle pre- and post-processing.

        This function should look like an if sieve on the `process_type` and fall back with a call to
        this method if no processing needs to be done.

        Parameters
        ----------
        data :
            The data to be processed.
        process_type :
            One of `pdf_preprocess`, `pdf_postprocess`, `ppf_preprocess`, `ppf_post_process`.
        ranges :
            Upper and lower bounds of the distribution support.

        Returns
        -------
            The processed data.
        """
        return data

    def pdf(self, x: pd.Series) -> Union[np.ndarray, pd.Series]:
        x = self.process(x, "pdf_preprocess", self._range)
        pdf = self.distribution(**self._parameter_data).pdf(x)
        return self.process(pdf, "pdf_postprocess", self._range)

    def ppf(self, x: pd.Series) -> Union[np.ndarray, pd.Series]:
        x = self.process(x, "ppf_preprocess", self._range)
        ppf = self.distribution(**self._parameter_data).ppf(x)
        return self.process(ppf, "ppf_postprocess", self._range)


class Beta(BaseDistribution):

    distribution = stats.beta

    def _get_params(self, data: pd.DataFrame) -> Dict[str, pd.DataFrame]:
        data_mean, data_sd = data['mean'], data['standard_deviation']
        scale = self._range['x_max'] - self._range['x_min']
        a = 1 / scale * (data_mean - self._range['x_min'])
        b = (1 / scale * data_sd) ** 2
        shape_1 = a ** 2 / b * (1 - a) - a
        shape_2 = a / b * (1 - a) ** 2 + (a - 1)
        params = {'scale': pd.DataFrame(scale, index=data.index),
                  'a': pd.DataFrame(shape_1, index=data.index),
                  'b': pd.DataFrame(shape_2, index=data.index)}
        return params

    def process(self, data: Union[np.ndarray, pd.Series], process_type: str,
                ranges: Dict[str, np.ndarray]) -> np.array:
        if process_type == 'pdf_preprocess':
            value = data - ranges['x_min']
        elif process_type == 'ppf_postprocess':
            value = data + ranges['x_max'] - ranges['x_min']
        else:
            value = super().process(data, process_type, ranges)
        return np.array(value)


class Exponential(BaseDistribution):

    distribution = stats.expon

    def _get_params(self, data: pd.DataFrame) -> Dict[str, pd.DataFrame]:
        return {'scale': pd.DataFrame(data['mean'], index=data.index)}


class Gamma(BaseDistribution):

    distribution = stats.gamma

    def _get_params(self, data: pd.DataFrame) -> Dict[str, pd.DataFrame]:
        mean, sd = data['mean'], data['standard_deviation']
        a = (mean / sd) ** 2
        scale = sd ** 2 / mean
        return {'a': pd.DataFrame(a, index=data.index), 'scale': pd.DataFrame(scale, index=data.index)}


class Gumbel(BaseDistribution):

    distribution = stats.gumbel_r

    def _get_params(self, data: pd.DataFrame) -> Dict[str, pd.DataFrame]:
        mean, sd = data['mean'], data['standard_deviation']
        loc = mean - (np.euler_gamma * np.sqrt(6) / np.pi * sd)
        scale = np.sqrt(6) / np.pi * sd
        return {'loc': pd.DataFrame(loc, index=data.index),
                'scale': pd.DataFrame(scale, index=data.index)}


class InverseGamma(BaseDistribution):

    distribution = stats.invgamma

    def _get_params(self, data: pd.DataFrame) -> Dict[str, pd.DataFrame]:
        def f(guess, mean, sd):
            alpha, beta = np.abs(guess)
            mean_guess = beta / (alpha - 1)
            var_guess = beta ** 2 / ((alpha - 1) ** 2 * (alpha - 2))
            return (mean - mean_guess) ** 2 + (sd ** 2 - var_guess) ** 2

        opt_results = _get_optimization_result(data, f, lambda m, s: np.array((m, m * s)))
        data_size = len(data)

        if not np.all([opt_results[k].success for k in range(data_size)]):
            raise NonConvergenceError('InverseGamma did not converge!!', 'invgamma')

        shape = np.abs([opt_results[k].x[0] for k in range(data_size)])
        scale = np.abs([opt_results[k].x[1] for k in range(data_size)])
        return {'a': pd.DataFrame(shape, index=data.index), 'scale': pd.DataFrame(scale, index=data.index)}


class InverseWeibull(BaseDistribution):

    distribution = stats.invweibull

    def _get_params(self, data: pd.DataFrame) -> Dict[str, pd.DataFrame]:
        # moments from  Stat Papers (2011) 52: 591. https://doi.org/10.1007/s00362-009-0271-3
        # it is much faster than using stats.invweibull.mean/var
        def f(guess, mean, sd):
            shape, scale = np.abs(guess)
            mean_guess = scale * special.gamma(1 - 1 / shape)
            var_guess = scale ** 2 * special.gamma(1 - 2 / shape) - mean_guess ** 2
            return (mean - mean_guess) ** 2 + (sd ** 2 - var_guess) ** 2

        opt_results = _get_optimization_result(data, f, lambda m, s: np.array((max(2.2, s / m), m)))
        data_size = len(data)

        if not np.all([opt_results[k].success for k in range(data_size)]):
            raise NonConvergenceError('InverseWeibull did not converge!!', 'invweibull')

        shape = np.abs([opt_results[k].x[0] for k in range(data_size)])
        scale = np.abs([opt_results[k].x[1] for k in range(data_size)])
        return {'c': pd.DataFrame(shape, index=data.index), 'scale': pd.DataFrame(scale, index=data.index)}


class LogLogistic(BaseDistribution):

    distribution = stats.burr12

    def _get_params(self, data: pd.DataFrame) -> Dict[str, pd.DataFrame]:
        def f(guess, mean, sd):
            shape, scale = np.abs(guess)
            b = np.pi / shape
            mean_guess = scale * b / np.sin(b)
            var_guess = scale ** 2 * 2 * b / np.sin(2 * b) - mean_guess ** 2
            return (mean - mean_guess) ** 2 + (sd ** 2 - var_guess) ** 2

        opt_results = _get_optimization_result(data, f, lambda m, s: np.array((max(2, m), m)))
        data_size = len(data)

        if not np.all([opt_results[k].success for k in range(data_size)]):
            raise NonConvergenceError('LogLogistic did not converge!!', 'llogis')

        shape = np.abs([opt_results[k].x[0] for k in range(data_size)])
        scale = np.abs([opt_results[k].x[1] for k in range(data_size)])
        return {'c': pd.DataFrame(shape, index=data.index),
                'd': pd.DataFrame([1]*len(data), index=data.index),
                'scale': pd.DataFrame(scale, index=data.index)}


class LogNormal(BaseDistribution):

    distribution = stats.lognorm

    def _get_params(self, data: pd.DataFrame) -> Dict[str, pd.DataFrame]:
        mean, sd = data['mean'], data['standard_deviation']
        alpha = 1 + sd ** 2 / mean ** 2
        s = np.sqrt(np.log(alpha))
        scale = mean / np.sqrt(alpha)
        return {'s': pd.DataFrame(s, index=data.index),
                'scale': pd.DataFrame(scale, index=data.index)}


class MirroredGumbel(BaseDistribution):

    distribution = stats.gumbel_r

    def _get_params(self, data: pd.DataFrame) -> Dict[str, pd.DataFrame]:
        loc = self._range['x_max'] - data['mean'] - (
                    np.euler_gamma * np.sqrt(6) / np.pi * data['standard_deviation'])
        scale = np.sqrt(6) / np.pi * data['standard_deviation']
        return {'loc': pd.DataFrame(loc, index=data.index),
                'scale': pd.DataFrame(scale, index=data.index)}

    def process(self, data: Union[np.ndarray, pd.Series], process_type: str,
                ranges: Dict[str, np.ndarray]) -> np.ndarray:
        if process_type == 'pdf_preprocess':
            value = ranges['x_max'] - data
        elif process_type == 'ppf_preprocess':
            value = 1 - data
        elif process_type == 'ppf_postprocess':
            value = ranges['x_max'] - data
        else:
            value = super().process(data, process_type, ranges)
        return np.array(value)


class MirroredGamma(BaseDistribution):

    distribution = stats.gamma

    def _get_params(self, data: pd.DataFrame) -> Dict[str, pd.DataFrame]:
        mean, sd = data['mean'], data['standard_deviation']
        a = ((self._range['x_max'] - mean) / sd) ** 2
        scale = sd ** 2 / (self._range['x_max'] - mean)
        return {'a': pd.DataFrame(a, index=data.index), 'scale': pd.DataFrame(scale, index=data.index)}

    def process(self, data: Union[np.ndarray, pd.Series], process_type: str,
                ranges: Dict[str, np.ndarray]) -> np.ndarray:
        if process_type == 'pdf_preprocess':
            value = ranges['x_max'] - data
        elif process_type == 'ppf_preprocess':
            value = 1 - data
        elif process_type == 'ppf_postprocess':
            value = ranges['x_max'] - data
        else:
            value = super().process(data, process_type, ranges)
        return np.array(value)


class Normal(BaseDistribution):

    distribution = stats.norm

    def _get_params(self, data: pd.DataFrame) -> Dict[str, pd.DataFrame]:
        return {'loc': pd.DataFrame(data['mean'], index=data.index),
                'scale': pd.DataFrame(data['standard_deviation'], index=data.index)}


class Weibull(BaseDistribution):

    distribution = stats.weibull_min

    def _get_params(self, data: pd.DataFrame) -> Dict[str, pd.DataFrame]:
        def f(guess, mean, sd):
            shape, scale = np.abs(guess)
            mean_guess = scale * special.gamma(1 + 1 / shape)
            var_guess = scale ** 2 * special.gamma(1 + 2 / shape) - mean_guess ** 2
            return (mean - mean_guess) ** 2 + (sd ** 2 - var_guess) ** 2

        opt_results = _get_optimization_result(data, f, lambda m, s: np.array((m, m / s)))
        data_size = len(data)

        if not np.all([opt_results[k].success is True for k in range(data_size)]):
            raise NonConvergenceError('Weibull did not converge!!', 'weibull')

        shape = np.abs([opt_results[k].x[0] for k in range(data_size)])
        scale = np.abs([opt_results[k].x[1] for k in range(data_size)])
        return {'c': pd.DataFrame(shape, index=data.index), 'scale': pd.DataFrame(scale, index=data.index)}


class EnsembleDistribution:
    """Represents an arbitrary distribution as a weighted sum of several concrete distribution types."""

    def __init__(self, data, weights, distribution_map):
        self.weights, self._distributions = self.get_valid_distributions(data, weights, distribution_map)

    @staticmethod
    def get_valid_distributions(data: pd.DataFrame, weights: pd.Series,
                                distribution_map: Dict) -> Tuple[np.ndarray, Dict]:
        """Produces a distribution that filters out non convergence errors and rescales weights appropriately.

        Parameters
        ----------
        data :
            Table where each row has a `mean` and `standard_deviation` for a single distribution.
        weights :
            A list of normalized distribution weights indexed by distribution type.
        distribution_map :
            Mapping between distribution name and distribution class.

        Returns
        -------
            Rescaled weights and the subset of the distribution map corresponding to convergent distributions.
        """
        dist = dict()
        for key in distribution_map:
            try:
                dist[key] = distribution_map[key](data)
            except NonConvergenceError as e:
                if weights[e.dist] > 0.05:
                    weights = weights.drop(e.dist)
                else:
                    raise NonConvergenceError(f'Divergent {key} distribution has weights: {100*weights[key]}%', key)

        return weights/np.sum(weights), dist

    def pdf(self, x: pd.Series) -> Union[np.ndarray, pd.Series]:
        return np.sum([self.weights[name] * dist.pdf(x)
                       for name, dist in self._distributions.items()], axis=0)

    def ppf(self, x: pd.Series) -> Union[np.ndarray, pd.Series]:
        if not x.empty:
            datas = []
            for name, dist in self._distributions.items():
                datas.append(self.weights[name] * dist.ppf(x))
            return np.sum(datas, axis=0)
        else:
            return np.array([])