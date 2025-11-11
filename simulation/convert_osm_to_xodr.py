##############################################################################
###################									        ##################
###################			ADA Van Project			        ##################
###################			Nov 10, 2025			       	##################
###################									        ##################
###################		Convert from osm to xodr	        ##################
###################											##################
##############################################################################
#
# Sample use: convert_osm_to_xodr("ashby_ave_berkeley.osm", "ashby_ave_berkeley.xodr",3.66)
#

import argparse
import os
import sys

# Attempt to import the CARLA module. 
# This script requires the CARLA PythonAPI to be installed or accessible in your environment.
try:
    # Append the CARLA PythonAPI path if not already in system path 
    # NOTE: You may need to customize this path for your CARLA installation!
    # Example for Linux: /path/to/carla/PythonAPI/carla/dist/carla-0.9.15-py3.7-linux-x86_64.egg
    # sys.path.append(os.path.join(os.environ.get('CARLA_ROOT', ''), 'PythonAPI/carla/dist/carla-0.9.15-py3.7-linux-x86_64.egg'))
    
    import carla
except ImportError:
    print("Error: The 'carla' module is not found.")
    print("Please ensure the CARLA PythonAPI is installed or the path to the .egg file is correct.")
    sys.exit(1)


def convert_osm_to_xodr(osm_file_path, xodr_file_path, lane_width=4.0):
    """
    Reads an OSM file, converts its content to OpenDRIVE (.xodr) format
    using the CARLA API, and saves the result.

    Args:
        osm_file_path (str): Path to the input .osm file.
        xodr_file_path (str): Path where the output .xodr file will be saved.
        lane_width (float): The desired default width for lanes in meters.
    """
    if not os.path.exists(osm_file_path):
        print(f"Error: Input file not found at {osm_file_path}")
        return

    print(f"Starting conversion for: {osm_file_path}")
    print(f"Using default lane width: {lane_width} meters.")
    
    # 1. Read the .osm data
    try:
        # Use 'r' mode and 'utf-8' encoding for maximum compatibility with OSM files
        with open(osm_file_path, 'r', encoding='utf-8') as f:
            osm_data = f.read()
    except Exception as e:
        print(f"Error reading OSM file: {e}")
        return

    # 2. Define conversion settings
    settings = carla.Osm2OdrSettings()
    
    # Set the desired lane width
    settings.default_lane_width = lane_width
    
    # RECOMMENDED SETTINGS FOR CARLA
    # To prevent issues with overlapping walls/barriers in the simulation.
    settings.wall_height = 0.0 
    
    # Setting an offset is crucial for large maps to move the center 
    # of the map close to the Unreal Engine origin (0, 0, 0), reducing 
    # floating-point precision issues. You usually set this based on 
    # the centroid of the map area. Using 0.0 is the simplest default.
    settings.use_offsets = False 
    settings.offset_x = 0.0
    settings.offset_y = 0.0
    
    # Enable traffic light generation (optional, quality depends on OSM data)
    settings.generate_traffic_lights = True

    # Limit road types to those typically drivable in CARLA
    settings.set_osm_way_types([
        "motorway", "motorway_link", "trunk", "trunk_link", "primary", 
        "primary_link", "secondary", "secondary_link", "tertiary", 
        "tertiary_link", "unclassified", "residential"
    ])

    # 3. Convert to .xodr
    print("Converting OSM to OpenDRIVE (XODR)...")
    try:
        xodr_data = carla.Osm2Odr.convert(osm_data, settings)
    except Exception as e:
        print(f"An error occurred during CARLA conversion. This may be due to malformed OSM data or complexity.")
        print(f"CARLA Error: {e}")
        return

    # 4. Save the OpenDRIVE file
    try:
        with open(xodr_file_path, 'w', encoding='utf-8') as f:
            f.write(xodr_data)
        print(f"\nSuccess! OpenDRIVE file saved to: {xodr_file_path}")
    except Exception as e:
        print(f"Error saving XODR file: {e}")

