"""
Time Series anomaly detection functions.
Implementation inspired by the paper https://arxiv.org/pdf/1802.04431.pdf
"""

import numpy as np
import pandas as pd
from scipy.optimize import fmin


def regression_errors(y, y_hat, smoothing_window=0.01, smooth=True):
    """Compute an array of absolute errors comparing predictions and expected output.

    If smooth is True, apply EWMA to the resulting array of errors.

    Args:
        y (array): Ground truth.
        y_hat (array): Predictions array.
        smoothing_window (float): Size of the smoothing window, expressed as a proportion
            of the total length of y.
        smooth (bool): whether the returned errors should be smoothed with EWMA.

    Returns:
        (array): errors
    """
    errors = np.abs(y - y_hat)[:, 0]

    if not smooth:
        return errors

    smoothing_window = int(smoothing_window * len(y))

    return pd.Series(errors).ewm(span=smoothing_window).mean().values


def deltas(errors, epsilon, mean, std):
    """Compute mean and std deltas.

    delta_mean = mean(errors) - mean(all errors below epsilon)
    delta_std = std(errors) - std(all errors below epsilon)
    """
    below = errors[errors <= epsilon]
    if not len(below):
        return 0, 0

    return mean - below.mean(), std - below.std()


def count_above(errors, epsilon):
    """Count number of errors and continuous sequences above epsilon.

    Continuous sequences are counted by shifting and counting the number
    of positions where there was a change and the original value was true,
    which means that a sequence started at that position.
    """
    above = errors > epsilon
    total_above = len(errors[above])

    above = pd.Series(above)
    shift = above.shift(1)
    change = above != shift

    total_consecutive = sum(above & change)

    return total_above, total_consecutive


def z_cost(z, errors, mean, std):
    """Compute how bad a z value is.

    The original formula is::

                 (delta_mean/mean) + (delta_std/std)
        ------------------------------------------------------
        number of errors above + (number of sequences above)^2

    which computes the "goodness" of `z`, meaning that the higher the value
    the better the `z`.

    In this case, we return this value inverted (we make it negative), to convert
    it into a cost function, as later on we will use scipy to minimize it.
    """

    epsilon = mean + z * std

    delta_mean, delta_std = deltas(errors, epsilon, mean, std)
    above, consecutive = count_above(errors, epsilon)

    numerator = -(delta_mean / mean + delta_std / std)
    denominator = above + consecutive ** 2

    if denominator == 0:
        return np.inf

    return numerator / denominator


def _find_threshold(errors, z_range=(0, 10)):
    """Find the ideal threshold.

    The ideal threshold is the one that minimizes the z_cost function.
    """

    mean = errors.mean()
    std = errors.std()

    min_z, max_z = z_range
    best_z = min_z
    best_cost = np.inf
    for z in range(min_z, max_z):
        best = fmin(z_cost, z, args=(errors, mean, std), full_output=True, disp=False)
        z, cost = best[0:2]
        if cost < best_cost:
            best_z = z[0]

    return mean + best_z * std


def _find_sequences(errors, epsilon, anomaly_interval):
    """Find sequences of values that are above epsilon.

    This is done following this steps:

        * create a boolean mask that indicates which values are above epsilon.
        * fill certain area around those values with a True to mark anomalous sequence.
        * shift this mask by one place, filing the empty gap with a False.
        * compare the shifted mask with the original one to see if there are changes.
        * Consider a sequence start any point which was true and has changed.
        * Consider a sequence end any point which was false and has changed.
    """
    above = pd.Series(errors > epsilon)
    index_above = np.argwhere(above)

    for i in index_above.flatten():
        above[max(0, i - anomaly_interval):min(i + anomaly_interval + 1, len(above))] = True

    shift = above.shift(1).fillna(False)
    change = above != shift

    if above.all():
        max_below = 0
    else:
        max_below = max(errors[~above])

    index = above.index
    starts = index[above & change].tolist()
    ends = (index[~above & change] - 1).tolist()

    if len(ends) == len(starts) - 1:
        ends.append(len(above) - 1)

    return np.array([starts, ends]).T, max_below


def _get_max_errors(errors, sequences, max_below):
    """Get the maximum error for each anomalous sequence.

    Also add a row with the max error which was not considered anomalous.

    Table containing a ``max_error`` column with the maximum error of each
    sequence and a column ``sequence`` with the corresponding start and stop
    indexes, sorted descendingly by errors.
    """
    max_errors = [{
        'max_error': max_below,
        'start': -1,
        'stop': -1
    }]

    for sequence in sequences:
        start, stop = sequence
        sequence_errors = errors[start: stop + 1]
        max_errors.append({
            'start': start,
            'stop': stop,
            'max_error': max(sequence_errors)
        })

    max_errors = pd.DataFrame(max_errors).sort_values('max_error', ascending=False)

    return max_errors.reset_index(drop=True)


def _prune_anomalies(max_errors, min_percent):
    """Prune anomalies to mitigate false positives.

    This is done by following these steps:

        * Shift the errors 1 negative step to compare each value with the next one.
        * Drop the last row, which we do not want to compare.
        * Calculate the percentage increase for each row.
        * Find rows which are below ``min_percent``.
        * Find the index of the latest of such rows.
        * Get the values of all the sequences above that index.
    """
    next_error = max_errors['max_error'].shift(-1).iloc[:-1]
    max_error = max_errors['max_error'].iloc[:-1]

    increase = (max_error - next_error) / max_error
    too_small = increase < min_percent

    if too_small.all():
        last_index = -1
    else:
        last_index = max_error[~too_small].index[-1]

    return max_errors[['start', 'stop', 'max_error']].iloc[0: last_index + 1].values


def _compute_scores(pruned_anomalies, errors, threshold, window_start):
    """Compute the score of the anomalies and add window_start timestamp to make the index absolute.
    """
    anomalies = list()
    denominator = errors.mean() + errors.std()

    for row in pruned_anomalies:
        max_error = row[2]
        score = (max_error - threshold) / denominator
        anomalies.append([row[0] + window_start, row[1] + window_start, score])

    return anomalies


def _merge_sequences(sequences):
    """Merge consecutive and overlapping sequences.

    We iterate over a list of start, end, score triples and merge together
    overlapping or consecutive sequences.
    The score of a merged sequence is the average of the single scores,
    weighted by the length of the corresponding sequences.
    """

    sorted_sequences = sorted(sequences, key=lambda entry: entry[0])
    new_sequences = []
    score = list()
    weights = list()

    for i in sorted_sequences:
        if not new_sequences:
            score.append(i[2])
            weights.append(i[1] - i[0])
            new_sequences.append(i)
        else:
            j = new_sequences[-1]
            if i[0] <= j[1] + 1:
                score.append(i[2])
                weights.append(i[1] - i[0])
                weighted_average = np.average(score, weights=weights)
                new_sequences[-1] = (j[0], max(j[1], i[1]), weighted_average)
            else:
                score = [i[2]]
                weights = [i[1] - i[0]]
                new_sequences.append(i)

    return np.array(new_sequences)


def find_anomalies(errors, index, z_range=(0, 10), window_size=None, window_step_size=None,
                   min_percent=0.1, anomaly_interval=50, lower_threshold=False):
    """Find sequences of values that are anomalous.

    We first find the ideal threshold for the set of errors that we have,
    and then find the sequences of values that are above this threshold.

    Lastly, we compute a score proportional to the maximum error in the
    sequence, and finally return the index pairs that correspond to
    each sequence, along with its score.
    """

    window_size = window_size or len(errors)
    window_step_size = window_step_size or len(errors)
    window_start = 0
    window_end = 0
    sequences = list()

    while window_end < len(errors):
        window_end = window_start + window_size
        window = errors[window_start:window_end]
        threshold = _find_threshold(window, z_range)
        window_sequences, max_below = _find_sequences(window, threshold, anomaly_interval)
        max_errors = _get_max_errors(window, window_sequences, max_below)
        pruned_anomalies = _prune_anomalies(max_errors, min_percent)
        window_sequences = _compute_scores(pruned_anomalies, window, threshold, window_start)
        sequences.extend(window_sequences)

        if lower_threshold:
            # Flip errors sequence around mean
            mean = window.mean()
            inverted_window = mean - (window - mean)

            # Perform same procedure as above
            threshold = _find_threshold(inverted_window, z_range)
            window_sequences, max_below = _find_sequences(inverted_window, threshold,
                                                          anomaly_interval)
            max_errors = _get_max_errors(inverted_window, window_sequences, max_below)
            pruned_anomalies = _prune_anomalies(max_errors, min_percent)
            window_sequences = _compute_scores(pruned_anomalies, window, threshold, window_start)
            sequences.extend(window_sequences)

        window_start = window_start + window_step_size

    sequences = _merge_sequences(sequences)

    anomalies = list()
    for start, stop, score in sequences:
        anomalies.append([index[int(start)], index[int(stop)], score])

    return np.asarray(anomalies)
