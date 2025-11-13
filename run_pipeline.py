from models.experiments.video_play_test import convert_svo_to_video
from models.experiments.parallelized_inference import run_yolo_sliced_video_parallel
from feature_processing.zed_depth_sensing import zed_depth_sense_from_svo
import configparser

config_file = 'config.ini'
config = configparser.ConfigParser()
config.read(config_file)

INPUT_SVO2 = config.get('SourceData', 'input_svo2')
INPUT_GPS = config.get('SourceData', 'input_gps')
CONVERTED_MP4_DIR = config.get('SourceData', 'converted_mp4_dir')

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


def main():
    """Executes the full computer vision processing pipeline."""
    start_time = time.time()

    print(f"1. Converting SVO file to MP4: {INPUT_SVO2} -> {CONVERTED_MP4_DIR}")
    try:
        convert_svo_to_video(INPUT_SVO2, CONVERTED_MP4_DIR)
        print("SVO to MP4 conversion complete.")
    except Exception as e:
        print(f"Error during SVO to MP4 conversion: {e}")
        return

    print(f"2. Generating 3D Point Cloud: {INPUT_SVO2} -> {POINT_CLOUD_OUTPUT_DIR}")
    try:
        zed_depth_sense_from_svo(
            svo_file=INPUT_SVO2,
            output_file=POINT_CLOUD_OUTPUT_DIR
            #max_dist=MAX_DISTANCE,
            #min_dist=MIN_DISTANCE
        )
        print("3D Point Cloud generation complete.")
    except Exception as e:
        print(f"Error during Depth Sensing: {e}")

    print(f"3. Running Parallelized YOLO Inference on Video: {CONVERTED_MP4_DIR}")
    try:
        yolo_output_dir = "models/experiments/yolo_detections_output" 
        os.makedirs(yolo_output_dir, exist_ok=True)
        
        run_yolo_sliced_video_parallel(
            input_video_path=CONVERTED_MP4_DIR,
            model_path="models/experiments/best.pt",
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
    main()