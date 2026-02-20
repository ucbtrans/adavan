from models.experiments.video_play_test1 import convert_svo_to_video
from models.experiments.inference1 import run_yolo_sliced_video
from feature_processing.zed_depth_sensing import zed_depth_sense_from_svo
from models.experiments.avi_to_mp4 import convert_avi_to_mp4
import configparser
import argparse  # Added for command line arguments
import time
import os
import shutil
from pathlib import Path


config_file = 'config1.ini'
config = configparser.ConfigParser()
config.read(config_file)


#INPUT_GPS = config.get('SourceData', 'input_gps')

MAX_DISTANCE = config.getint('Depth', 'max_distance')
MIN_DISTANCE = config.getint('Depth', 'min_distance')
POINT_CLOUD_OUTPUT_DIR = config.get('Depth', '3D_cloud_output_dir')

GRANULARITY = config.getint('AbsLocation', 'granularity')
ABS_LOCATION_DIR = config.get('AbsLocation', 'abs_location_dir')

# Modeling parameters
NUM_SLICES_SAHI = config.getint('Modeling', 'num_slices_sahi')
SLICE_OVERLAP = config.getfloat('Modeling', 'slice_overlap')
CONFIDENCE_BOUND = config.getfloat('Modeling', 'confidence_bound')

DETECTION_MODALITY_STR = config.get('Modeling', 'detection_modality')
DETECTION_MODALITY = [item.strip().strip("'\"") for item in DETECTION_MODALITY_STR.strip('[]').split(',')]
SNAKE_MAKE_STEP = "step1"

def main(input_file,output_dir):
    """Executes the full computer vision processing pipeline."""
    start_time = time.time()

    print(f"1. Converting SVO file to MP4: {input_file} -> {output_dir}")
    try:
        # Get output file

        filename = os.path.basename(input_file)
        file_root = os.path.splitext(filename)[0]

        output_mp4 = output_dir + "/" + file_root + ".mp4"
        convert_svo_to_video(input_file, output_mp4)

        print("SVO to MP4 conversion complete.")
    except Exception as e:
        print(f"Error during SVO to MP4 conversion: {e}")
        return

    print(f"2. Running YOLO Inference on Video: {input_video} to {yolo_output_dir}")
    detections = []
    try:        
        detections = run_yolo_sliced_video(
            model_path="models/experiments/best.pt",
            input_video_path=input_video,
            output_video_path=yolo_output_dir
            # num_slices=NUM_SLICES_SAHI,
            # overlap_ratio=SLICE_OVERLAP,
            # conf_threshold=CONFIDENCE_BOUND,
            # target_classes=DETECTION_MODALITY
        )
        print(f"YOLO Inference complete. Results saved in: {yolo_output_dir}")
    except Exception as e:
        print(f"Error during YOLO Inference: {e}")
        return
    
    # --- 3. Convert AVI to MP4 and CLEANUP ---
    final_annotated_mp4 = os.path.join(output_dir, f"{file_root}_annotated.mp4")
    convert_avi_to_mp4(temp_avi, final_annotated_mp4)
    
    if os.path.exists(temp_avi):
        os.remove(temp_avi)
        print(f"Success: Deleted temporary AVI: {temp_avi}")


    print(f"3. Generating 3D Point Cloud: {input_file} -> {POINT_CLOUD_OUTPUT_DIR}")

    try:
        zed_depth_sense_from_svo(input_file)
            #svo_file=input_file,
            #output_file=POINT_CLOUD_OUTPUT_DIR
            #max_dist=MAX_DISTANCE,
            #min_dist=MIN_DISTANCE
        #)
        print("3D Point Cloud generation complete.")
    except Exception as e:
        print(f"Error during Depth Sensing: {e}")

    print("\nAbsolute Positioning")
    # # Example call when implemented:
    # try:
    #     run_absolute_positioning(
    #         gps_data=INPUT_GPS,
    #         detections_data=yolo_output_dir, 
    #         output_dir=ABS_LOCATION_DIR,
    #         granularity=GRANULARITY
    #     )
    #     print("   ✅ Absolute Positioning complete.")
    # except Exception as e:
    #     print(f"   ❌ Error during Absolute Positioning: {e}")


    with open(config_file, 'w') as configfile:
        config.write(configfile)
    print(f"\n--- Configuration saved to {config_file} ---")

    end_time = time.time()
    print(f"--- Pipeline Finished in {end_time - start_time:.2f} seconds ---")

if __name__ == "__main__":
    
    # Set up the argument parser
    parser = argparse.ArgumentParser(description="Run pipeline")
    
    # Define lowercase parameters
    parser.add_argument("input_file", help="Path to the input .svo2 file")
    parser.add_argument("output_dir", help="Path to the output .mp4 file")

    args = parser.parse_args()
    main(args.input_file, args.output_dir)
