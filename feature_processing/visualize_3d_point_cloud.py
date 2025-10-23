import pyzed.sl as sl
import math
import numpy as np
import sys
import open3d as o3d 

def main():
    # --- CONFIGURATION ---
    # Set to your SVO file path (or leave commented out for live camera)
    svo_file_path = "/home/ashwinb/Downloads/22-08-2025_23-44-58.svo2" 
    use_svo = True  # Change to True to use the SVO file
    # ---------------------

    # Create a Camera object
    zed = sl.Camera()

    # Create a InitParameters object and set configuration parameters
    init_params = sl.InitParameters()
    
    if use_svo:
        if not svo_file_path or svo_file_path == "path/to/your/file.svo":
            print("ERROR: Please set a valid path for your SVO file or set 'use_svo = False'.")
            exit()
        init_params.set_from_svo_file(svo_file_path)
        init_params.svo_real_time_mode = False # Playback as fast as possible
        print(f"Opening SVO file: {svo_file_path}")
    else:
        print("Opening live ZED camera...")

    # Set common parameters
    init_params.depth_mode = sl.DEPTH_MODE.PERFORMANCE  # Use NEURAL depth mode
    init_params.coordinate_units = sl.UNIT.MILLIMETER  # Use millimeter units for depth
    init_params.camera_resolution = sl.RESOLUTION.HD720

    # Open the camera
    status = zed.open(init_params)
    if status != sl.ERROR_CODE.SUCCESS:
        print("Camera Open : "+repr(status)+". Exit program.")
        exit()

    # Create and set RuntimeParameters
    runtime_parameters = sl.RuntimeParameters()
    
    # Mat objects
    point_cloud = sl.Mat()

    # Initialize Open3D visualizer objects
    vis = o3d.visualization.Visualizer()
    vis.create_window(window_name='ZED Point Cloud Visualization (Open3D)', width=1280, height=720)
    
    # Create an Open3D PointCloud object
    pcd = o3d.geometry.PointCloud()
    is_first_frame = True

    print("\n--- Starting Point Cloud Visualization (Press Q in the 3D window to exit) ---")
    
    while True: 
        grab_status = zed.grab(runtime_parameters)
        
        if grab_status == sl.ERROR_CODE.SUCCESS:
            # Retrieve colored point cloud.
            zed.retrieve_measure(point_cloud, sl.MEASURE.XYZRGBA)
            
            # Convert ZED sl.Mat to a NumPy array (H, W, 4)
            pc_np = point_cloud.get_data()
            
            # --- 1. Process 3D Coordinates (X, Y, Z) ---
            # Create a boolean mask to find valid (non-infinite/NaN) points
            # We check the Z-coordinate as it's the most common channel to be invalid
            valid_indices = np.isfinite(pc_np[:, :, 2])
            
            # Extract only the valid 3D points (X, Y, Z)
            points_3d = pc_np[valid_indices][:, :3]
            
            # Update the Open3D PointCloud points
            pcd.points = o3d.utility.Vector3dVector(points_3d)

            # --- 2. Process Colors (R, G, B) ---
            # The RGBA data is packed into the 4th channel (index 3) of the ZED sl.Mat
            rgba = pc_np[valid_indices][:, 3]
            
            # Extract R, G, B channels and normalize them to the [0, 1] range required by Open3D
            colors_np = np.zeros((points_3d.shape[0], 3), dtype=np.float32)
            colors_np[:, 0] = (rgba // (256**2)) % 256  # Red
            colors_np[:, 1] = (rgba // 256) % 256      # Green
            colors_np[:, 2] = rgba % 256               # Blue
            
            pcd.colors = o3d.utility.Vector3dVector(colors_np / 255.0)

            # --- 3. Update Visualizer ---
            if is_first_frame:
                # Add the point cloud to the visualizer for the very first frame
                vis.add_geometry(pcd)
                is_first_frame = False
            else:
                # Update the point cloud data for subsequent frames
                vis.update_geometry(pcd)
                
            # Render the scene and process user interactions (like mouse movement or 'Q' key press)
            vis.poll_events()
            vis.update_renderer()
            
            # Optional: Check if the user closed the window or pressed 'Q'
            if not vis.poll_events(): 
                 break 

        elif grab_status == sl.ERROR_CODE.END_OF_SVOFILE_REACHED:
            print("SVO end has been reached. Exiting 3D viewer.")
            break
        
        elif grab_status != sl.ERROR_CODE.SUCCESS:
             # Handle other potential errors (e.g., Camera not ready)
            pass

    # Clean up and close
    vis.destroy_window()
    zed.close()
    print("ZED camera closed and visualizer destroyed.")

if __name__ == "__main__":
    main()
