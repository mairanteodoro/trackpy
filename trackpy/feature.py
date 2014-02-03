# Copyright 2012 Daniel B. Allan
# dallan@pha.jhu.edu, daniel.b.allan@gmail.com
# http://pha.jhu.edu/~dallan
# http://www.danallan.com
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or (at
# your option) any later version.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, see <http://www.gnu.org/licenses>.


from __future__ import division
import warnings
import numpy as np
import pandas as pd
from scipy import ndimage
from scipy import stats
from pandas import DataFrame, Series
import matplotlib.pyplot as plt  # for walkthrough

from trackpy import uncertainty
from trackpy.preprocessing import bandpass, scale_to_gamut
from .C_fallback_python import nullify_secondary_maxima
from .utils import record_meta, print_update
from .masks import *
import trackpy  # to get trackpy.__version__

from .try_numba import try_numba_autojit, NUMBA_AVAILABLE


def local_maxima(image, radius, separation, percentile=64):
    """Find local maxima whose brightness is above a given percentile."""

    # Compute a threshold based on percentile.
    not_black = image[np.nonzero(image)]
    threshold = stats.scoreatpercentile(not_black, percentile)
    ndim = image.ndim

    # The intersection of the image with its dilation gives local maxima.
    if not np.issubdtype(image.dtype, np.integer):
        raise TypeError("Perform dilation on exact (i.e., integer) data.")
    footprint = binary_mask(radius, ndim, separation)
    dilation = ndimage.grey_dilation(image, footprint=footprint,
                                     mode='constant')
    maxima = np.where((image == dilation) & (image > threshold))
    if not np.size(maxima) > 0:
        _warn_no_maxima()
        return np.empty((0, ndim))

    # Flat peaks, for example, return multiple maxima. Eliminate them.
    maxima_map = np.zeros_like(image)
    maxima_map[maxima] = image[maxima]
    footprint = binary_mask(separation, ndim, separation)
    maxima_map = ndimage.generic_filter(
        maxima_map, nullify_secondary_maxima(), footprint=footprint,
        mode='constant')
    maxima = np.where(maxima_map > 0)

    # Do not accept peaks near the edges.
    margin = int(separation)//2
    maxima_map[..., -margin:] = 0
    maxima_map[..., :margin] = 0
    if ndim > 1:
        maxima_map[..., -margin:, :] = 0
        maxima_map[..., :margin, :] = 0
    if ndim > 2:
        maxima_map[..., -margin:, :, :] = 0
        maxima_map[..., :margin, :, :] = 0
    if ndim > 3:
        raise NotImplementedError("I tap out beyond three dimensions.")
        # TODO Change if into loop using slice(None) as :
    maxima = np.where(maxima_map > 0)
    if not np.size(maxima) > 0:
        warnings.warn("Bad image! All maxima were in the margins.",
                      UserWarning)

    # Return coords in as a numpy array shaped so it can be passed directly
    # to the DataFrame constructor.
    return np.vstack(maxima).T


def estimate_mass(image, radius, coord):
    "Compute the total brightness in the neighborhood of a local maximum."
    square = [slice(c - radius, c + radius + 1) for c in coord]
    neighborhood = binary_mask(radius, image.ndim)*image[square]
    return np.sum(neighborhood)


def estimate_size(image, radius, coord, estimated_mass):
    "Compute the total brightness in the neighborhood of a local maximum."
    square = [slice(c - radius, c + radius + 1) for c in coord]
    neighborhood = binary_mask(radius, image.ndim)*image[square]
    Rg = np.sqrt(np.sum(r_squared_mask(radius, image.ndim)*neighborhood)/
                 estimated_mass)
    return Rg

# center_of_mass can have divide-by-zero errors, avoided thus:
def _safe_center_of_mass(x, radius):
    result = np.array(ndimage.center_of_mass(x))
    if np.isnan(result).any():
        return np.zeros_like(result) + radius
    else:
        return result


def refine(raw_image, image, radius, coords, max_iterations=10, engine='auto',
           characterize=True, walkthrough=False):
    """Find the center of mass of a bright feature starting from an estimate.

    Characterize the neighborhood of a local maximum, and iteratively
    hone in on its center-of-brightness. Return its coordinates, integrated
    brightness, size (Rg), eccentricity (0=circular), and signal strength.
    
    Parameters
    ----------
    raw_image : array (any dimensions)
        used for final characterization
    image : array (any dimension)
        processed image, used for locating center of mass
    coord : array
        estimated position
    max_iterations : integer
        max number of loops to refine the center of mass, default 10
    characterize : boolean, True by default
        Compute and return mass, size, eccentricity, signal.
    walkthrough : boolean, False by default
        Print the offset on each loop and display final neighborhood image.
    engine : {'python', 'numba'}
        Numba is faster if available, but it cannot do walkthrough.
    """
    # Main loop will be performed in separate function.
    if engine == 'auto':
        if NUMBA_AVAILABLE:
            engine = 'numba'
        else:
            engine = 'python'
    if engine == 'python':
        coords = np.array(coords)  # a copy, will not modify in place
        results = _refine(raw_image, image, radius, coords, max_iterations, 
                          characterize, walkthrough)
    elif engine == 'numba':
        if not NUMBA_AVAILABLE:
            warnings.warn("numba could not be imported. Without it, the "
                          "'numba' engine runs very slow. Use the 'python' "
                          "engine or install numba.", UserWarning)
        if image.ndim != 2:
            raise NotImplementedError("The numba engine only supports 2D " 
                                      "images. You can extend it if you feel "
                                      "like a hero.")
        if walkthrough:
            raise ValueError("walkthrough is not availabe in the nubma engine")
        # Do some extra prep in pure Python that can't be done in numba.
        coords = np.array(coords, dtype=np.float_)
        shape = np.array(image.shape, dtype=np.int16)  # array, not tuple
        mask = binary_mask(radius, image.ndim)
        r2_mask = r_squared_mask(radius, image.ndim)
        cmask = cosmask(radius)
        smask = sinmask(radius)
        results = _numba_refine(raw_image, image, int(radius), coords,
                                int(max_iterations), characterize,
                                shape, mask, r2_mask, cmask, smask)
    else:
        raise ValueError("Available engines are 'python' and 'numba'")
    return results


# (This is pure Python. A numba variant follows below.)
def _refine(image, raw_image, radius, coords, max_iterations,
            characterize, walkthrough):
    SHIFT_THRESH = 0.6
    GOOD_ENOUGH_THRESH = 0.01

    ndim = image.ndim
    mask = binary_mask(radius, ndim)
    slices = [[slice(c - radius, c + radius + 1) for c in coord]
              for coord in coords]

    # Declare arrays that we will fill iteratively through loop.
    N = coords.shape[0]
    final_coords = np.empty_like(coords, dtype=np.float64)
    mass = np.empty(N, dtype=np.float64)
    Rg = np.empty(N, dtype=np.float64)
    ecc = np.empty(N, dtype=np.float64)
    signal = np.empty(N, dtype=np.float64)

    for feat in range(N):
        coord = coords[feat]

        # Define the circular neighborhood of (x, y).
        square = slices[feat]
        neighborhood = mask*image[square]
        cm_n = _safe_center_of_mass(neighborhood, radius)
        cm_i = cm_n - radius + coord  # image coords
        allow_moves = True
        for iteration in range(max_iterations):
            off_center = cm_n - radius
            if walkthrough:
                print off_center
            if np.all(np.abs(off_center) < GOOD_ENOUGH_THRESH):
                break  # Accurate enough.

            # If we're off by more than half a pixel in any direction, move.
            elif np.any(np.abs(off_center) > SHIFT_THRESH) & allow_moves:
                # In here, coord is an integer.
                new_coord = coord
                new_coord[off_center > SHIFT_THRESH] += 1
                new_coord[off_center < -SHIFT_THRESH] -= 1
                # Don't move outside the image!
                upper_bound = np.array(image.shape) - 1 - radius
                new_coord = np.clip(new_coord, radius, upper_bound).astype(int)
                # Update slice to shifted position.
                square = [slice(c - radius, c + radius + 1) for c in new_coord]
                neighborhood = mask*image[square]

            # If we're off by less than half a pixel, interpolate.
            else:
                # Here, coord is a float. We are off the grid.
                neighborhood = ndimage.shift(neighborhood, -off_center, 
                                             order=2, mode='constant', cval=0)
                new_coord = coord + off_center
                # Disallow any whole-pixels moves on future iterations.
                allow_moves = False

            cm_n = _safe_center_of_mass(neighborhood, radius)  # neighborhood
            cm_i = cm_n - radius + new_coord  # image coords
            coord = new_coord
        # matplotlib and ndimage have opposite conventions for xy <-> yx.
        final_coords[feat] = cm_i[..., ::-1]

        if walkthrough:
            plt.imshow(neighborhood)

        # Characterize the neighborhood of our final centroid.
        mass[feat] = neighborhood.sum()
        if not characterize:
            continue  # short-circuit loop
        Rg[feat] = np.sqrt(np.sum(r_squared_mask(radius, ndim)*
                                  neighborhood)/mass[feat])
        # I only know how to measure eccentricity in 2D.
        if ndim == 2:
            ecc[feat] = np.sqrt(np.sum(neighborhood*cosmask(radius))**2 +
                          np.sum(neighborhood*sinmask(radius))**2)
            ecc[feat] /= (mass[feat] - neighborhood[radius, radius] + 1e-6)
        else:
            ecc[feat] = np.nan
        raw_neighborhood = mask*raw_image[square]
        signal[feat] = raw_neighborhood.max()  # black_level subtracted later

    if not characterize:
        result = np.column_stack([final_coords, mass])
    else:
        result = np.column_stack([final_coords, mass, Rg, ecc, signal])
    return result

def _get_numba_refine_locals():
    """Establish types of local variables in _numba_refine(), in a way that's safe if there's no numba."""
    try:
        from numba import double, int_
    except ImportError:
        return {}
    else:
        return dict(square0=int_, square1=int_, square_size=int_, Rg_=double, ecc_=double)

@try_numba_autojit(locals=_get_numba_refine_locals())
def _numba_refine(image, raw_image, radius, coords, max_iterations,
                  characterize, shape, mask, r2_mask, cmask, smask):
    SHIFT_THRESH = 0.6
    GOOD_ENOUGH_THRESH = 0.01

    square_size = 2*radius + 1

    # Declare arrays that we will fill iteratively through loop.
    N = coords.shape[0]
    final_coords = np.empty_like(coords, dtype=np.float_)
    mass = np.empty(N, dtype=np.float_)
    Rg = np.empty(N, dtype=np.float_)
    ecc = np.empty(N, dtype=np.float_)
    signal = np.empty(N, dtype=np.float_)
    coord = np.empty((2,), dtype=np.float_)

    # Buffer arrays
    cm_n = np.empty(2, dtype=np.float_)
    cm_i = np.empty(2, dtype=np.float_)
    off_center = np.empty(2, dtype=np.float_)
    new_coord = np.empty((2,), dtype=np.int_)

    for feat in range(N):
        # Define the circular neighborhood of (x, y).
        for dim in range(2):
            coord[dim] = coords[feat, dim]
            cm_n[dim] = 0.
        square0 = coord[0] - radius
        square1 = coord[1] - radius
        mass_ = 0.0
        for i in range(square_size):
            for j in range(square_size):
                if mask[i, j] != 0:
                    px = image[square0 + i, square1 + j]
                    cm_n[0] += px*i
                    cm_n[1] += px*j
                    mass_ += px

        for dim in range(2):
            cm_n[dim] /= mass_
            cm_i[dim] = cm_n[dim] - radius + coord[dim]
        allow_moves = True
        for iteration in range(max_iterations):
            for dim in range(2):
                off_center[dim] = cm_n[dim] - radius
            for dim in range(2):
                if off_center[dim] > GOOD_ENOUGH_THRESH:
                    break  # Proceed through iteration.
                break  # Stop iterations.

            # If we're off by more than half a pixel in any direction, move.
            do_move = False
            if allow_moves:
                for dim in range(2):
                    if off_center[dim] > SHIFT_THRESH:
                        do_move = True
            do_move = True

            if do_move:
                # In here, coord is an integer.
                for dim in range(2):
                    new_coord[dim] = int(round(coord[dim]))
                    oc = off_center[dim]
                    if oc > SHIFT_THRESH:
                        new_coord[dim] += 1
                    elif oc < - SHIFT_THRESH:
                        new_coord[dim] -= 1
                    # Don't move outside the image!
                    if new_coord[dim] < radius:
                        new_coord[dim] = radius
                    upper_bound = shape[dim] - radius - 1
                    if new_coord[dim] > upper_bound:
                        new_coord[dim] = upper_bound
                # Update slice to shifted position.
                square0 = new_coord[0] - radius
                square1 = new_coord[1] - radius
                for dim in range(2):
                     cm_n[dim] = 0.

            # If we're off by less than half a pixel, interpolate.
            else:
                break
                # TODO Implement this for numba.
                # Remember to zero cm_n somewhere in here.
                # Here, coord is a float. We are off the grid.
                # neighborhood = ndimage.shift(neighborhood, -off_center, 
                #                              order=2, mode='constant', cval=0)
                # new_coord = np.float_(coord) + off_center
                # Disallow any whole-pixels moves on future iterations.
                # allow_moves = False

            # cm_n was re-zeroed above in an unrelated loop
            mass_ = 0.
            for i in range(square_size):
                for j in range(square_size):
                    if mask[i, j] != 0:
                        px = image[square0 + i, square1 + j]
                        cm_n[0] += px*i
                        cm_n[1] += px*j
                        mass_ += px

            for dim in range(2):
                cm_n[dim] /= mass_
                cm_i[dim] = cm_n[dim] - radius + coord[dim]
                coord[dim] = new_coord[dim]
        # matplotlib and ndimage have opposite conventions for xy <-> yx.
        final_coords[feat, 0] = cm_i[1]
        final_coords[feat, 1] = cm_i[0]

        # Characterize the neighborhood of our final centroid.
        mass_ = 0.
        Rg_ = 0.
        ecc1 = 0.
        ecc2 = 0.
        signal_ = 0.
        for i in range(square_size):
            for j in range(square_size):
                if mask[i, j] != 0:
                    px = image[square0 + i, square1 + j]
                    mass_ += px
                    # Will short-circuiting if characterize=False slow it down?
                    if not characterize:
                        continue
                    Rg_ += r2_mask[i, j]*px
                    ecc1 += cmask[i, j]*px
                    ecc2 += smask[i, j]*px
                    raw_px = raw_image[square0 + i, square1 + j]
                    if raw_px > signal_:
                        signal_ = px
        Rg_ = np.sqrt(Rg_/mass_)
        mass[feat] = mass_
        if characterize:
            Rg[feat] = Rg_
            center_px = image[square0 + radius, square1 + radius]
            ecc_ = np.sqrt(ecc1**2 + ecc2**2)/(mass_ - center_px + 1e-6)
            ecc[feat] = ecc_
            signal[feat] = signal_  # black_level subtracted later

    if not characterize:
        result = np.column_stack([final_coords, mass])
    else:
        result = np.column_stack([final_coords, mass, Rg, ecc, signal])
    return result


def locate(raw_image, diameter, minmass=100., maxsize=None, separation=None,
           noise_size=1, smoothing_size=None, threshold=1, invert=False,
           percentile=64, topn=None, preprocess=True, max_iterations=10,
           filter_before=True, filter_after=True, 
           characterize=True, engine='auto'):
    """Locate Gaussian-like blobs of a given approximate size.

    Preprocess the image by performing a band pass and a threshold.
    Locate all peaks of brightness, characterize the neighborhoods of the peaks
    and take only those with given total brightnesss ("mass"). Finally,
    refine the positions of each peak.

    Parameters
    ----------
    image : image array (any dimensions)
    diameter : feature size in px
    minmass : minimum integrated brightness
        Default is 100, but a good value is often much higher. This is a
        crucial parameter for elminating spurrious features.
    maxsize : maximum radius-of-gyration of brightness, default None
    separation : feature separation, in pixels
        Default is the feature diameter + 1.
    noise_size : width of Gaussian blurring kernel, in pixels
        Default is 1.
    smoothing_size : size of boxcar smoothing, in pixels
        Default is the same is feature separation.
    threshold : Clip bandpass result below this value.
        Default 1; use 8 for 16-bit images.
    invert : Set to True if features are darker than background. False by
        default.
    percentile : Features must have a peak brighter than pixels in this
        percentile. This helps eliminate spurrious peaks.
    topn : Return only the N brightest features above minmass. 
        If None (default), return all features above minmass.

    Returns
    -------
    DataFrame([x, y, mass, size, ecc, signal])
        where mass means total integrated brightness of the blob,
        size means the radius of gyration of its Gaussian-like profile,
        and ecc is its eccentricity (1 is circular).

    Other Parameters
    ----------------
    preprocess : Set to False to turn out bandpass preprocessing.
    max_iterations : integer
        max number of loops to refine the center of mass, default 10
    filter_before : boolean
        Use minmass (and maxsize, if set) to eliminate spurrious features
        based on their estimated mass and size before refining position.
        True by default for performance.
    filter_after : boolean
        Use final characterizations of mass and size to elminate spurrious
        features. True by default.
    characterize : boolean
        Compute "extras": eccentricity, signal, ep. True by default.
    engine : {'auto', 'python', 'numba'}

    See Also
    --------
    batch : performs location on many images in batch

    Notes
    -----
    This is an implementation of the Crocker-Grier centroid-finding algorithm.
    [1]_

    References
    ----------
    .. [1] Crocker, J.C., Grier, D.G. http://dx.doi.org/10.1006/jcis.1996.0217

    """

    # Validate parameters and set defaults.
    if not diameter & 1:
        raise ValueError("Feature diameter must be an odd number. Round up.")
    if not separation:
        separation = int(diameter) + 1
    radius = int(diameter)//2
    if smoothing_size is None:
        smoothing_size = diameter
    raw_image = np.squeeze(raw_image)
    if preprocess:
        if invert:
            # It is tempting to do this in place, but if it is called multiple
            # times on the same image, chaos reigns.
            max_value = np.iinfo(raw_image.dtype).max
            raw_image = raw_image ^ max_value
        image = bandpass(raw_image, noise_size, smoothing_size, threshold)
    else:
        image = raw_image.copy()
    # Coerce the image into integer type. Rescale to fill dynamic range.
    if np.issubdtype(raw_image.dtype, np.integer):
        dtype = raw_image.dtype
    else:
        dtype = np.int8
    image = scale_to_gamut(image, dtype)

    # Find local maxima.
    coords = local_maxima(image, radius, separation, percentile)
    count_maxima = coords.shape[0]

    # Proactively filter based on estimated mass/size before
    # refining positions.
    if filter_before:
        approx_mass = np.empty(count_maxima)  # initialize to avoid appending
        for i in range(count_maxima):
            approx_mass[i] = estimate_mass(image, radius, coords[i])
        condition = approx_mass > minmass
        if maxsize is not None:
            approx_size = np.empty(count_maxima)
            for i in range(count_maxima):
                approx_size[i] = estimate_size(image, radius, coords[i], 
                                               approx_mass[i])
            condition &= approx_size < maxsize
        coords = coords[condition]
    count_qualified = coords.shape[0]

    # Refine their locations and characterize mass, size, etc.
    refined_coords = refine(raw_image, image, radius, coords, max_iterations,
                            engine, characterize)

    # Filter again, using final ("exact") mass -- and size, if set.
    MASS_COLUMN_INDEX = image.ndim
    SIZE_COLUMN_INDEX = image.ndim + 1
    exact_mass = refined_coords[:, MASS_COLUMN_INDEX]
    if filter_after:
        condition = exact_mass > minmass
        if maxsize is not None:
            exact_size = refined_coords[:, SIZE_COLUMN_INDEX]
            condition &= exact_size < maxsize
        refined_coords = refined_coords[condition]
        exact_mass = exact_mass[condition]  # used below by topn
    count_qualified = refined_coords.shape[0]

    if topn is not None and count_qualified > topn:
        if topn == 1:
            # special case for high performance and correct shape
            refined_coords = refined_coords[np.argmax(exact_mass)]
            refined_coords = refined_coords.reshape(1, -1)
        else:
            refined_coords = refined_coords[np.argsort(exact_mass)][-topn:]

    # Put the results in a DataFrame.
    if image.ndim < 4:
        coord_columns = ['x', 'y', 'z'][:image.ndim]
    else:
        coord_columns = map(lambda i: 'x' + str(i), range(image.ndim))
    char_columns = ['mass']
    if characterize:
        char_columns += ['size', 'ecc', 'signal']
    columns = coord_columns + char_columns
    if len(refined_coords) == 0:
        return DataFrame(columns=columns)  # TODO fill with np.empty
    f = DataFrame(refined_coords, columns=columns)

    # Estimate the uncertainty in position using signal (measured in refine)
    # and noise (measured here below).
    if characterize:
        black_level, noise = uncertainty.measure_noise(
            raw_image, diameter, threshold)
        f['signal'] -= black_level
        ep = uncertainty.static_error(f, noise, diameter, noise_size)
        f = f.join(ep)
    return f


def batch(frames, diameter, minmass=100, maxsize=None, separation=None,
          noise_size=1, smoothing_size=None, threshold=1, invert=False,
          percentile=64, topn=None, preprocess=True, max_iterations=10,
          filter_before=True, filter_after=True,
          characterize=True, engine='auto',
          store=None, conn=None, sql_flavor=None, table=None,
          do_not_return=False, meta=True):
    """Locate Gaussian-like blobs of a given approximate size.

    Preprocess the image by performing a band pass and a threshold.
    Locate all peaks of brightness, characterize the neighborhoods of the peaks
    and take only those with given total brightnesss ("mass"). Finally,
    refine the positions of each peak.

    Parameters
    ----------
    frames : list (or iterable) of images
    diameter : feature size in px
    minmass : minimum integrated brightness
        Default is 100, but a good value is often much higher. This is a
        crucial parameter for elminating spurrious features.
    maxsize : maximum radius-of-gyration of brightness, default None
    separation : feature separation, in pixels
        Default is the feature diameter + 1.
    noise_size : width of Gaussian blurring kernel, in pixels
        Default is 1.
    smoothing_size : size of boxcar smoothing, in pixels
        Default is the same is feature separation.
    threshold : Clip bandpass result below this value.
        Default 1; use 8 for 16-bit images.
    invert : Set to True if features are darker than background. False by
        default.
    percentile : Features must have a peak brighter than pixels in this
        percentile. This helps eliminate spurrious peaks.
    topn : Return only the N brightest features above minmass. 
        If None (default), return all features above minmass.

    Returns
    -------
    DataFrame([x, y, mass, size, ecc, signal])
        where mass means total integrated brightness of the blob,
        size means the radius of gyration of its Gaussian-like profile,
        and ecc is its eccentricity (1 is circular).

    Other Parameters
    ----------------
    preprocess : Set to False to turn off bandpass preprocessing.
    max_iterations : integer
        max number of loops to refine the center of mass, default 10
    filter_before : boolean
        Use minmass (and maxsize, if set) to eliminate spurrious features
        based on their estimated mass and size before refining position.
        True by default for performance.
    filter_after : boolean
        Use final characterizations of mass and size to elminate spurrious
        features. True by default.
    characterize : boolean
        Compute "extras": eccentricity, signal, ep. True by default.
    engine : {'auto', 'python', 'numba'}

    store : Optional HDFStore
    conn : Optional connection to a SQL database
    sql_flavor : If using a SQL connection, specify 'sqlite' or 'MySQL'.
    table : If using HDFStore or SQL, specify table name.
        Default: 'features_timestamp'.
    do_not_return : Save the result frame by frame, but do not return it when
        finished. Conserved memory for parallel jobs.
    meta : By default, a YAML (plain text) log file is saved in the current
        directory. You can specify a different filepath set False.

    See Also
    --------
    locate : performs location on a single image

    Notes
    -----
    This is an implementation of the Crocker-Grier centroid-finding algorithm.
    [1]_

    References
    ----------
    .. [1] Crocker, J.C., Grier, D.G. http://dx.doi.org/10.1006/jcis.1996.0217
    
    """
    # Gather meta information and save as YAML in current directory.
    timestamp = pd.datetime.utcnow().strftime('%Y-%m-%d-%H%M%S')
    try:
        source = frames.filename
    except:
        source = None
    meta_info = dict(timestamp=timestamp,
                     trackpy_version=trackpy.__version__,
                     source=source, diameter=diameter, minmass=minmass, 
                     maxsize=maxsize, separation=separation, 
                     noise_size=noise_size, smoothing_size=smoothing_size, 
                     invert=invert, percentile=percentile, topn=topn, 
                     preprocess=preprocess, max_iterations=max_iterations,
                     filter_before=filter_before, filter_after=filter_after,
                     store=store, conn=conn, 
                     sql_flavor=sql_flavor, table=table,
                     do_not_return=do_not_return)
    if meta:
        if isinstance(meta, str):
            filename = meta
        else:
            filename = 'feature_log_%s.yml' % timestamp
        record_meta(meta_info, filename)

    all_centroids = []
    for i, image in enumerate(frames):
        # If frames has a cursor property, use it. Otherwise, just count
        # the frames from 0.
        try:
            frame_no = frames.cursor - 1
        except AttributeError:
            frame_no = i
        centroids = locate(image, diameter, minmass, maxsize, separation,
                           noise_size, smoothing_size, threshold, invert,
                           percentile, topn, preprocess, max_iterations,
                           filter_before, filter_after, characterize,
                           engine)
        centroids['frame'] = frame_no
        message = "Frame %d: %d features" % (frame_no, len(centroids))
        print_update(message)
        if len(centroids) == 0:
            continue
        indexed = ['frame']  # columns on which you can perform queries

        # HDF Mode: Save iteratively in pandas HDFStore table.
        if store is not None:
            store.append(table, centroids, data_columns=indexed)
            store.flush()  # Force save. Not essential.

        # SQL Mode: Save iteratively in SQL table.
        elif conn is not None:
            if sql_flavor is None:
                raise ValueError("Specifiy sql_flavor: MySQL or sqlite.")
            pd.io.sql.write_frame(centroids, table, conn,
                                  flavor=sql_flavor, if_exists='append')

        # Simple Mode: Accumulate all results in memory and return.
        else:
            all_centroids.append(centroids)

    if do_not_return:
        return None
    if store is not None:
        try:
            store.get_storer(table).attrs.meta = meta
            return store[table]
        except MemoryError:
            raise MemoryError("The batch was completed and saved " +
                              "successfully but it is too large to return " +
                              "en masse at this time.") 
    elif conn is not None:
        try:
            return pd.io.sql.read_frame("SELECT * FROM %s" % table, conn)
        except MemoryError:
            raise MemoryError("The batch was completed and saved " +
                              "successfully but it is too large to return " +
                              "en masse at this time.") 
    else:
        return pd.concat(all_centroids).reset_index(drop=True)


def _warn_no_maxima():
    warnings.warn("No local maxima were found.", UserWarning)