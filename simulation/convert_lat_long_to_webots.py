import requests
import xml.etree.ElementTree as ET
from pyproj import Proj, Transformer
import os

def download_osm_by_bbox(min_lat, min_lon, max_lat, max_lon, output_file="map.osm"):
    """
    Downloads an .osm file from the Overpass API using a bounding box.
    Order: South, West, North, East
    """
    print(f"Fetching OSM data for bounding box: {min_lat}, {min_lon}, {max_lat}, {max_lon}...")
    # Overpass API interpreter URL
    url = "https://overpass-api.de/api/interpreter"
    
    # Query to get all data within the bounding box
    query = f"""
    [out:xml][timeout:25];
    (
      node({min_lat},{min_lon},{max_lat},{max_lon});
      way({min_lat},{min_lon},{max_lat},{max_lon});
      relation({min_lat},{min_lon},{max_lat},{max_lon});
    );
    out body;
    >;
    out skel qt;
    """
    
    try:
        response = requests.post(url, data={'data': query})
        response.raise_for_status()
        with open(output_file, 'wb') as f:
            f.write(response.content)
        print(f"Successfully saved to {output_file}")
        return True
    except Exception as e:
        print(f"Error downloading OSM data: {e}")
        return False

def get_osm_center_and_bounds(osm_file):
    """
    Parses the .osm file to find the bounding box and calculates the center.
    """
    tree = ET.parse(osm_file)
    root = tree.getroot()
    bounds = root.find('bounds')
    
    if bounds is not None:
        minlat = float(bounds.get('minlat'))
        minlon = float(bounds.get('minlon'))
        maxlat = float(bounds.get('maxlat'))
        maxlon = float(bounds.get('maxlon'))
        
        center_lat = (minlat + maxlat) / 2
        center_lon = (minlon + maxlon) / 2
        
        return {
            "minlat": minlat, "minlon": minlon, 
            "maxlat": maxlat, "maxlon": maxlon,
            "center_lat": center_lat, "center_lon": center_lon
        }
    else:
        raise ValueError("Could not find <bounds> tag in OSM file. Try a different export method.")

def latlong_to_webots_pos(target_lat, target_lon, osm_file):
    """
    Converts a lat/long to a Webots X, Y coordinate relative to the map center.
    """
    # 1. Get map metadata
    data = get_osm_center_and_bounds(osm_file)
    
    # 2. Check if target is within bounds
    is_inside = (data['minlat'] <= target_lat <= data['maxlat'] and 
                 data['minlon'] <= target_lon <= data['maxlon'])
    
    if not is_inside:
        print(f"Warning: Target ({target_lat}, {target_lon}) is OUTSIDE map boundaries!")
        print(f"Boundaries: Lat [{data['minlat']}, {data['maxlat']}], Lon [{data['minlon']}, {data['maxlon']}]")

    # 3. Setup Projection (UTM is best for local meter-based accuracy)
    # Determine UTM zone automatically based on longitude
    utm_zone = int((data['center_lon'] + 180) / 6) + 1
    projection_str = f"+proj=utm +zone={utm_zone} +ellps=WGS84 +datum=WGS84 +units=m +no_defs"
    
    transformer = Transformer.from_crs("epsg:4326", projection_str, always_xy=True)
    
    # 4. Convert Center and Target to UTM (meters)
    center_x, center_y = transformer.transform(data['center_lon'], data['center_lat'])
    target_x, target_y = transformer.transform(target_lon, target_lat)
    
    # 5. Calculate relative position
    # In Webots (ENU): X = Easting offset, Y = Northing offset
    webots_x = target_x - center_x
    webots_y = target_y - center_y
    
    return webots_x, webots_y, is_inside

# --- EXAMPLE USAGE ---
if __name__ == "__main__":
    # 1. Define your area (Bounding Box)
    # Example: A small area in Paris
    S, W, N, E = 48.8580, 2.2940, 48.8590, 2.2955
    osm_filename = "paris_tower.osm"
    
    # 2. Download the file via API
    if not os.path.exists(osm_filename):
        download_osm_by_bbox(S, W, N, E, osm_filename)
    
    # 3. Target coordinate to find in the simulation
    # Let's pick a spot near the center of that box
    my_lat, my_lon = 48.8585, 2.2945
    
    x, y, inside = latlong_to_webots_pos(my_lat, my_lon, osm_filename)
    
    print("-" * 30)
    print(f"Target Lat/Long: {my_lat}, {my_lon}")
    print(f"Webots Position (X, Y): {x:.3f}, {y:.3f}")
    print(f"Within Bounds: {inside}")
    print("-" * 30)
    print("PRO TIP: If your Webots ground is the XZ plane (Y is up),")
    print(f"use: translation {x:.3f} 0 {-y:.3f}")