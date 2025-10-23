import cv2
import numpy as np
import sys
import os
from ultralytics import YOLO # Needed for context, though SAHI loads it internally
from sahi import AutoDetectionModel 
from sahi.predict import get_sliced_prediction
import torch

# --- Configuration (Set your paths here) ---
MODEL_PATH = 'best.pt' 
INPUT_VIDEO_PATH = 'output_video.mp4' # Replace with the actual video file path
OUTPUT_VIDEO_PATH = 'annotated_output_1.avi'
DEVICE = 'cuda:0' if torch.cuda.is_available() else 'cpu'

# SAHI Slicing Parameters
SLICE_HEIGHT = 1024
SLICE_WIDTH = 1024
OVERLAP_RATIO = 0.2

# --- Core Video Processing Function ---

def run_yolo_sliced_video(model_path: str, input_video_path: str, output_video_path: str):
    """
    Reads a video frame-by-frame, runs SAHI sliced inference on the NumPy array 
    (in-memory), and writes the annotated frame to a new video file.
    No intermediate images or files are saved.
    """
    
    # 1. Initialize Video Reader and Writer
    cap = cv2.VideoCapture(input_video_path)
    if not cap.isOpened():
        print(f"Error: Could not open video file: {input_video_path}")
        return

    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = cap.get(cv2.CAP_PROP_FPS)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    # Setup VideoWriter
    fourcc = cv2.VideoWriter_fourcc(*'XVID') # Codec for MP4
    out = cv2.VideoWriter(output_video_path, fourcc, fps, (width, height))
    if not out.isOpened():
        print(f"Error: Could not open VideoWriter for output file: {output_video_path}")
        cap.release()
        return

    # 2. Initialize SAHI Model Wrapper
    try:
        # Load the model using Ultralytics' YOLO class first (more robust)
        ultralytics_model = YOLO(model_path)
        
        detection_model = AutoDetectionModel.from_pretrained(
            model=ultralytics_model, 
            model_type='yolov8',      # Specify the type
            confidence_threshold=0.1,
            device=DEVICE,
        )
        print(f"Model loaded successfully on {DEVICE}. Total frames: {total_frames}")
    except Exception as e:
        print(f"Error initializing detection model: {e}")
        cap.release()
        out.release()
        return

    # 3. Main Frame Processing Loop
    frame_count = 0
    while cap.isOpened():
        ret, frame = cap.read() # frame is a NumPy array (BGR format)

        if frame_count > 100:
            break

        if not ret:
            break

        frame_count += 1
        sys.stdout.write(f"\rProcessing frame {frame_count}/{total_frames}...")
        sys.stdout.flush()

        # Run SAHI sliced inference directly on the NumPy array
        # NOTE: SAHI requires RGB input, so convert the BGR frame
        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        
        result = get_sliced_prediction(
            image=frame_rgb,  # Pass NumPy array (in-memory)
            detection_model=detection_model,
            slice_height=SLICE_HEIGHT,
            slice_width=SLICE_WIDTH,
            overlap_height_ratio=OVERLAP_RATIO,
            overlap_width_ratio=OVERLAP_RATIO,
            postprocess_type='NMS'
        )

        # 4. Draw Bounding Boxes onto the Frame
        annotated_frame = frame.copy() # Start with the original BGR frame
        
        # Iterate over SAHI's prediction list
        for prediction in result.object_prediction_list:
            bbox = prediction.bbox.to_xyxy() # [x_min, y_min, x_max, y_max]
            score = prediction.score.value
            category_name = prediction.category.name
            
            x_min, y_min, x_max, y_max = [int(val) for val in bbox]
            
            # Draw rectangle (BGR color format)
            cv2.rectangle(annotated_frame, (x_min, y_min), (x_max, y_max), (0, 255, 0), 2)
            
            # Draw label
            label = f"{category_name}: {score:.2f}"
            cv2.putText(annotated_frame, label, (x_min, y_min - 10), 
                        cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 255, 0), 2)

        # 5. Write the annotated frame to the output video
        out.write(annotated_frame)

    # 6. Cleanup
    cap.release()
    out.release()
    print("\nVideo processing complete.")
    print(f"Annotated video saved to: {output_video_path}")
    print(f"Total frames processed: {frame_count}")


if __name__ == "__main__":
    # Ensure you replace 'input_video.mp4' with the actual path to your video source
    # If your SVO conversion created 'output_video.mp4', use that here.
    INPUT_VIDEO_SOURCE = 'output_video.mp4' 
    
    # You need to ensure the necessary imports are included at the top of your script
    # and that the libraries (cv2, sahi, ultralytics, torch) are installed.
    
    run_yolo_sliced_video(MODEL_PATH, INPUT_VIDEO_SOURCE, OUTPUT_VIDEO_PATH)