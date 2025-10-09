# This script requires the Stereolabs ZED SDK (specifically the pyzed wrapper)
# and OpenCV (cv2) to be installed in your Python environment.
# You can install OpenCV via pip: pip install opencv-python

import sys
import numpy as np
import cv2

# Import the ZED SDK (Ensure ZED SDK and pyzed are installed)
try:
    import pyzed.sl as sl
except ImportError:
    print("Error: ZED SDK (pyzed) not found.")
    print("Please install the ZED SDK and the Python wrapper.")
    sys.exit(1)


def convert_svo_to_video(svo_input_path, output_video_path):
    """
    Opens an SVO file, extracts the left video stream, and saves it as an MP4 file.
    
    Args:
        svo_input_path (str): Path to the input .svo2 file.
        output_video_path (str): Path where the output .mp4 file will be saved.
    """
    print(f"Attempting to open SVO file: {svo_input_path}")

    # 1. Setup ZED Camera Parameters for SVO Playback
    init_params = sl.InitParameters()
    init_params.set_from_svo_file(svo_input_path)
    # Ensure all data types are available in the SVO file
    init_params.svo_real_time_mode = False 
    init_params.coordinate_system = sl.COORDINATE_SYSTEM.IMAGE 
    #init_params.coordinate_unit = sl.UNIT.MILLIMETER

    # 2. Create and Open the ZED Camera/SVO Handler
    zed = sl.Camera()
    err = zed.open(init_params)
    if err != sl.ERROR_CODE.SUCCESS:
        print(f"Error opening ZED SVO file: {err}")
        zed.close()
        return

    # 3. Retrieve SVO Metadata for VideoWriter
    cam_info = zed.get_camera_information()

    svo_res = cam_info.camera_configuration.resolution
    svo_fps = cam_info.camera_configuration.fps
    width = svo_res.width
    height = svo_res.height
    
    print(f"SVO Resolution: {width}x{height}, FPS: {svo_fps}")

    # 4. Setup OpenCV VideoWriter
    # Define the codec (H.264 format) and create VideoWriter object
    # FourCC codes: 'mp4v' or 'XVID' for AVI, 'avc1' for MP4 (might require specific libs)
    fourcc = cv2.VideoWriter_fourcc(*'mp4v') 
    out = cv2.VideoWriter(output_video_path, fourcc, svo_fps, (width, height))
    
    if not out.isOpened():
        print(f"Error: Could not open VideoWriter for output file: {output_video_path}")
        zed.close()
        return

    # 5. Prepare data container and runtime parameters
    image_zed = sl.Mat()
    runtime_parameters = sl.RuntimeParameters()
    
    frame_count = 0
    total_frames = zed.get_svo_number_of_frames()

    print("Starting SVO conversion loop...")

    # 6. Main Frame Processing Loop
    while True:
        # Grab a frame from the SVO file
        if zed.grab(runtime_parameters) == sl.ERROR_CODE.SUCCESS:
            frame_count += 1
            
            # Retrieve the left view image (RGB)
            zed.retrieve_image(image_zed, sl.VIEW.LEFT) 
            
            # Convert ZED sl.Mat to a numpy array (OpenCV format)
            # .get_data() retrieves the data, .copy() ensures it's contiguous
            image_ocv = image_zed.get_data()
            
            # Convert RGBA (ZED default) to BGR (OpenCV default)
            frame_bgr = cv2.cvtColor(image_ocv, cv2.COLOR_RGBA2BGR)

            # Write the frame to the output video file
            out.write(frame_bgr)
            
            # Update progress
            sys.stdout.write(f"\rProcessing frame {frame_count}/{total_frames}...")
            sys.stdout.flush()

        else:
            break

    # 7. Cleanup
    print("\nConversion finished. Releasing resources.")
    out.release()
    zed.close()
    
    if frame_count > 0:
        print(f"Successfully converted {frame_count} frames to {output_video_path}")
    else:
        print("No frames were processed. Check SVO file integrity.")


if __name__ == "__main__":
    INPUT_FILE ="/home/ashwinb/Documents/adavan/models/experiments/21-09-2025_22-36-40.svo2" 
    OUTPUT_FILE = "output_video.mp4"
    convert_svo_to_video(INPUT_FILE, OUTPUT_FILE)
