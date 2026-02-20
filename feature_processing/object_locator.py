import pyzed.sl as sl
import pandas as pd
import numpy as np
from geopy.distance import geodesic
from geopy.point import Point
from scipy.interpolate import interp1d

def calculate_object_coordinates(gps_csv_path, detections, svo_path):
    """
    :param gps_csv_path: Path to CSV with columns [timestamp, lat, lon]
    :param detections: List of (timestamp, distance_m, x_offset_pixel) tuples
    :param svo_path: Path to the .svo2 file
    :return: List of (timestamp, obj_lat, obj_lon)
    """
    # 1. Load and prepare GPS interpolation
    df_gps = pd.read_csv(gps_csv_path)
    # Ensure timestamps are floats for interpolation
    interp_lat = interp1d(df_gps['timestamp'], df_gps['lat'], fill_value="extrapolate")
    interp_lon = interp1d(df_gps['timestamp'], df_gps['lon'], fill_value="extrapolate")

    # 2. Initialize ZED SVO to get Heading
    zed = sl.Camera()
    init_params = sl.InitParams()
    init_params.set_from_svo_file(svo_path)
    init_params.svo_real_time_mode = False 
    
    if zed.open(init_params) != sl.ERROR_CODE.SUCCESS:
        print("Failed to open SVO file")
        return []

    # Enable tracking to get fused orientation (Heading)
    zed.enable_positional_tracking(sl.PositionalTrackingParameters())
    
    # Get camera intrinsic (needed to turn pixel offset into degrees)
    calibration_params = zed.get_camera_information().camera_configuration.calibration_parameters
    focal_x = calibration_params.left_cam.fx
    
    results = []
    sensors_data = sl.SensorsData()

    for ts, distance, x_offset in detections:
        # A. Find Observer Lat/Lon at this exact timestamp
        obs_lat = float(interp_lat(ts))
        obs_lon = float(interp_lon(ts))
        
        # B. Find Camera Heading at this timestamp
        # Set SVO to the frame closest to our detection timestamp
        zed.set_svo_position(int(ts)) # This assumes TS maps to frame index or use zed.grab() logic
        
        if zed.grab() == sl.ERROR_CODE.SUCCESS:
            zed.get_sensors_data(sensors_data, sl.TIME_REFERENCE.IMAGE)
            # Get Yaw from IMU (Euler angles: [Roll, Yaw, Pitch])
            # Note: 0 is North in ZED coordinate system if Magnetometer is calibrated
            cam_heading = sensors_data.get_imu_data().get_pose().get_orientation().get_euler_angles()[1]
            
            # C. Adjust bearing for object offset in frame
            # angle = atan(pixel_offset / focal_length)
            offset_angle_deg = np.degrees(np.arctan(x_offset / focal_x))
            total_bearing = (cam_heading + offset_angle_deg) % 360

            # D. Project to absolute coordinates
            start_point = Point(obs_lat, obs_lon)
            dest = geodesic(meters=distance).destination(start_point, total_bearing)
            
            results.append((ts, dest.latitude, dest.longitude))

    zed.close()
    return results

# Example Usage:
# detections = [(1712345678.5, 12.5, -50), (1712345679.2, 8.0, 10)] 
# (timestamp, distance_in_meters, pixels_from_center_x)