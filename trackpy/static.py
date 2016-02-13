from scipy.spatial import cKDTree
import random
import numpy as np
from warnings import warn


def pairCorrelation_2d(feat, cutoff, fraction=1., dr=.5, p_indices=None, ndensity=None, boundary=None,
                            handle_edge=True):
    """   
    Calculate the pair correlation function in 2 dimensions.

    Parameters
    ----------
    feat : Pandas DataFrame
        DataFrame containing the x and y coordinates of particles
    cutoff : float
        Maximum distance to calculate g(r)
    fraction : float, optional
        The fraction of particles to calculate g(r) with. May be used to increase speed of function.
        Particles selected at random.
    dr : float, optional
        The bin width
    p_indices : sequence, optional
        Only consider a pair of particles if one of them is in 'p_indices'.
        Uses zero-based indexing, regardless of how 'feat' is indexed.
    ndensity : float, optional
        Density of particle packing. If not specified, density will be calculated assuming rectangular homogeneous
        arrangement.
    boundary : tuple, optional
        Tuple specifying rectangular boundary of particles (xmin, xmax, ymin, ymax). Must be floats.
        Default is to assume a rectangular packing. Boundaries are determined by edge particles.
    handle_edge : boolean, optional
        If true, compensate for reduced area around particles near the edges.

    Returns
    -------
    r_edges : array
        Return the bin edges
    g_r : array
        The values of g_r
    """

    if boundary is None:
        xmin, xmax, ymin, ymax =  feat.x.min(), feat.x.max(), feat.y.min(), feat.y.max()
    else:
        xmin, xmax, ymin, ymax = boundary
        # Disregard all particles outside the bounding box
        feat = feat[(feat.x >= xmin) & (feat.x <= xmax) & (feat.y >= ymin) & (feat.y <= ymax)]

    if ndensity is None:
        ndensity = feat.x.count() / ((xmax - xmin) * (ymax - ymin))  #  particle packing density

    if p_indices is None:
        p_indices = random.sample(range(len(feat)), int(fraction * len(feat)))  # grab random sample of particles

    r_edges = np.arange(0, cutoff + dr, dr)  # radii bins to search for particles
    g_r = np.zeros(len(r_edges) - 1) 
    max_p_count =  int(np.pi * (r_edges.max() + dr)**2 * ndensity * 10)  # upper bound for neighborhood particle count
    ckdtree = cKDTree(feat[['x', 'y']])  # initialize kdtree for fast neighbor search
    points = feat.as_matrix(['x', 'y'])  # Convert pandas dataframe to numpy array for faster indexing
        
    # For edge handling, two techniques are used. If a particle is near only one edge, the fractional area of the
    # search ring r+dr is caluclated analytically via 1 - arccos(d / r ) / pi, where d is the distance to the wall. If
    # the particle is near two or more walls, a ring of points is generated around the particle, and a mask is
    # applied to find the the number of points within the boundary, giving an estimate of the area. Below,
    # rings of size r + dr  for all r in r_edges are generated and cached for later use to speed up computation
    n = 1000  # TODO: Should scale with radius, dr
    refx, refy = _points_ring2D(r_edges, dr, n)

    for idx in p_indices:
        dist, idxs = ckdtree.query(points[idx], k=max_p_count, distance_upper_bound=cutoff)
        dist = dist[dist > 0] # We don't want to count the same particle

        area = np.pi * (np.arange(dr, cutoff + 2*dr, dr)**2 - np.arange(0, cutoff + dr, dr)**2)
        
        if handle_edge:
            # Find the number of edge collisions at each radii
            collisions = _num_wall_collisions2D(points[idx], r_edges, xmin, xmax, ymin, ymax)

            # If some disk will collide with the wall, we need to implement edge handling
            if np.any(collisions):

                # Use analyitcal solution to find area of disks cut off by one wall.
                # grab the distance to the closest wall
                d = _distances_to_wall2D(points[idx], xmin, xmax, ymin, ymax).min()

                inx = np.where(collisions == 1)[0]
                area[inx] *= 1 - np.arccos(d / (r_edges[inx] + dr/2)) / np.pi 
            
                # If disk is cutoff by 2 or more walls, generate a bunch of points and use a mask to estimate the area
                # within the boundaries
                inx = np.where(collisions >= 2)[0]
                x = refx[inx] + points[idx,0]
                y = refy[inx] + points[idx,1]
                mask = (x >= xmin) & (x <= xmax) & (y >= ymin) & (y <= ymax)
                area[inx] *= mask.sum(axis=1, dtype='float') / len(refx[0])
            
        g_r +=  np.histogram(dist, bins = r_edges)[0] / area[:-1]

    g_r /= (ndensity * len(p_indices))
    return r_edges, g_r



def pairCorrelation3D(feat, cutoff, fraction = 1., dr = .5, p_indices = None, ndensity=None, boundary = None,
                            handle_edge=True):
    """   
    Calculate the pair correlation function in 3 dimensions.

    Parameters
    ----------
    feat : Pandas DataFrame
        DataFrame containing the x, y and z coordinates of particles
    cutoff : float
        Maximum distance to calculate g(r)
    fraction : float, optional
        The fraction of particles to calculate g(r) with. May be used to increase speed of function. Particles selected at random.
    dr : float, optional
        The bin width
    p_indices : sequence, optional
        Only consider a pair of particles if one of them is in 'p_indices'.
        Uses zero-based indexing, regardless of how 'feat' is indexed.
    ndensity : float, optional
        Density of particle packing. If not specified, density will be calculated assuming rectangular homogenous
        arrangement.
    boundary : tuple, optional
        Tuple specifying rectangular prism boundary of particles (xmin, xmax, ymin, ymax, zmin, zmax). Must be floats.
        Default is to assume a rectangular packing. Boundaries are determined by edge particles.
    handle_edge : boolean, optional
        If true, compensate for reduced volume around particles near the edges.

    Returns
    -------
    r_edges : array
        Return the bin edges
    g_r : array
        The values of g_r
    """   

    if boundary is None:
        xmin, xmax, ymin, ymax, zmin, zmax = (feat.x.min(), feat.x.max(), feat.y.min(), feat.y.max(),
                                              feat.z.min(), feat.z.max())
    else:
        xmin, xmax, ymin, ymax, zmin, zmax = boundary

        # Disregard all particles outside the bounding box
        feat = feat[(feat.x >= xmin) & (feat.x <= xmax) & (feat.y >= ymin) & (feat.y <= ymax) &
                    (feat.z >= zmin) & (feat.z <= zmax)]

    if ndensity is None:
        ndensity = feat.x.count() / ((xmax - xmin) * (ymax - ymin) * (zmax - zmin)) #  particle packing density 

    if p_indices is None:
        p_indices = random.sample(range(len(feat)), int(fraction * len(feat)))  # grab random sample of particles

    r_edges = np.arange(0, cutoff + dr, dr)  # radii bins to search for particles
    g_r = np.zeros(len(r_edges) - 1)
    # Estimate upper bound for neighborhood particle count
    max_p_count =  int((4./3.) * np.pi * (r_edges.max() + dr)**3 * ndensity * 10)
    ckdtree = cKDTree(feat[['x', 'y', 'z']])  # initialize kdtree for fast neighbor search
    points = feat.as_matrix(['x', 'y', 'z'])  # Convert pandas dataframe to numpy array for faster indexing
        
    # For edge handling, two techniques are used. If a particle is near only one edge, the fractional area of the
    # search ring r+dr is caluclated analytically. If the particle is near two or more walls, a ring of points is 
    # generated around the particle, and a mask is applied to find the the number of points within the boundary, 
    # giving an estimate of the area. Below, rings of size r + dr  for all r in r_edges are generated and cached for 
    # later use to speed up computation.
    n = 1000  # TODO: Should scale with radius, dr
    refx, refy, refz = _points_ring3D(r_edges, dr, n)

    for idx in p_indices:
        dist, idxs = ckdtree.query(points[idx], k=max_p_count, distance_upper_bound=cutoff)
        dist = dist[dist > 0] # We don't want to count the same particle
    
        area = (4./3.) * np.pi * (np.arange(dr, cutoff + 2*dr, dr)**3 - np.arange(0, cutoff + dr, dr)**3)
        
        if handle_edge:
            # Find the number of edge collisions at each radii
            collisions = _num_wall_collisions3D(points[idx], r_edges, xmin, xmax, ymin, ymax, zmin, zmax)

            # If some disk will collide with the wall, we need to implement edge handling
            if np.any(collisions):

                # Use analyitcal solution to find area of disks cut off by one wall.
                # Grab the distance to the closest wall
                d = _distances_to_wall3D(points[idx], xmin, xmax, ymin, ymax, zmin, zmax).min()
                inx = np.where(collisions == 1)[0]

                theta = np.arccos(d / (r_edges[inx] + dr/2))
                area[inx] *= 1 - 2*np.pi*(1 - np.cos(theta)) / (4*np.pi)
            
                # If shell is cutoff by 2 or more walls, generate a bunch of points and use a mask to
                # estimate the area within the boundaries
                inx = np.where(collisions >= 2)[0]
                x = refx[inx] + points[idx,0]
                y = refy[inx] + points[idx,1]
                z = refz[inx] + points[idx,2]
                mask = (x >= xmin) & (x <= xmax) & (y >= ymin) & (y <= ymax) & (z >= zmin) & (z <= zmax)
                area[inx] *= mask.sum(axis=1, dtype='float') / len(refx[0])

        g_r +=  np.histogram(dist, bins = r_edges)[0] / area[:-1]

    g_r /= (ndensity * len(p_indices))
    return r_edges, g_r

def _num_wall_collisions2D(point, radius, xmin, xmax, ymin, ymax):
    """Returns the number of walls a shell of a certain radius and position collides with.
       Wall boundaries specified by min, max parameters"""
    collisions = (point[0] + radius >= xmax).astype(int) + (point[0] - radius <= xmin).astype(int) + \
                 (point[1] + radius >= ymax).astype(int) + (point[1] - radius <= ymin).astype(int)

    return collisions
    
def _distances_to_wall2D(point, xmin, xmax, ymin, ymax): 
    """Returns the distance of a paritlce a position 'point' to the nearest wall"""
    return np.array([point[0]-xmin, xmax-point[0], point[1]-ymin, ymax-point[1]])

def _points_ring2D(r_edges, dr, n):
    """Returns x, y array of points comprising shells extending from r to r_dr.
       n determines the number of points in each ring. Rings are generated by constructing 
       a unit disk and projecting every point onto a ring of thickness dr"""

    refx_all, refy_all = [],[]
    for r in r_edges:
        ref = 2*np.random.random(size=(n, 2)) - 1
        ref /= np.linalg.norm(ref, axis=1).repeat(2).reshape((len(ref), 2))
        ref *= dr*np.random.random(size=(len(ref), 2))+ r
        x,y = ref[:,0], ref[:,1]

        refx_all.append(x)
        refy_all.append(y)

    return np.array(refx_all), np.array(refy_all)


def _num_wall_collisions3D(point, radius, xmin, xmax, ymin, ymax, zmin, zmax):
    """Returns the number of walls a shell of a certain radius and position collides with.
       Wall boundaries specified by min, max parameters"""
    collisions = (point[0] + radius >= xmax).astype(int) + (point[0] - radius <= xmin).astype(int) + \
                 (point[1] + radius >= ymax).astype(int) + (point[1] - radius <= ymin).astype(int) + \
                 (point[2] + radius >= zmax).astype(int) + (point[2] - radius <= zmin).astype(int) 

    return collisions
    
def _distances_to_wall3D(point, xmin, xmax, ymin, ymax, zmin, zmax): 
    """Returns the distance of a paritlce a position 'point' to the nearest wall"""
    return np.array([point[0]-xmin, xmax-point[0], point[1]-ymin, ymax-point[1], point[2]-zmin, zmax-point[2]])

def _points_ring3D(r_edges, dr, n):
    """Returns x, y, z array of points comprising shells extending from r to r_dr. n determines the number of points in the ring.
        Rings are generated by constructing a unit sphere and projecting every point onto a shell of thickness dr"""

    refx_all, refy_all, refz_all = [],[],[]
    for r in r_edges:
        ref = 2*np.random.random(size=(n, 3)) - 1
        ref /= np.linalg.norm(ref, axis=1).repeat(3).reshape((len(ref), 3))
        ref *= dr*np.random.random(size=(len(ref), 3))+ r
        x,y,z = ref[:,0], ref[:,1], ref[:,2]

        refx_all.append(x)
        refy_all.append(y)
        refz_all.append(z)

    return np.array(refx_all), np.array(refy_all), np.array(refz_all)