import cv2
import numpy as np
import sys
import os
import torch
import multiprocessing as mp
from functools import partial
from ultralytics import YOLO
from sahi import AutoDetectionModel
from sahi.predict import get_sliced_prediction

MODEL_PATH = 'best.pt'
INPUT_VIDEO_PATH = 'output_video.mp4' # Replace with the actual video file path
OUTPUT_VIDEO_PATH = 'annotated_output_parallel.avi'
DEVICE = 'cuda:0' if torch.cuda.is_available() else 'cpu'

# SAHI Slicing Parameters
SLICE_HEIGHT = 540
SLICE_WIDTH = 960
OVERLAP_RATIO = 0.4

NUM_WORKERS = mp.cpu_count() - 2 # Keep some cores free for I/O processes

# --- Helper Functions for Parallel Pipeline ---

def init_detection_model(model_path: str, device: str):
    """
    Initializes and returns the SAHI detection model. This is called once 
    per worker process.
    """
    try:
        # Load the model using Ultralytics' YOLO class first (more robust)
        ultralytics_model = YOLO(model_path)

        detection_model = AutoDetectionModel.from_pretrained(
            model=ultralytics_model,
            model_type='yolov8',
            confidence_threshold=0.2,
            device=device,
        )
        return detection_model
    except Exception as e:
        print(f"Error initializing detection model in worker: {e}")
        return None

def process_frame(frame_data, detection_model):
    """
    Worker function to run SAHI inference on a single frame.
    frame_data is a tuple: (frame_count, frame_np_array)
    """
    frame_count, frame = frame_data
    if detection_model is None:
        return frame_count, frame # Return original frame if model failed to load

    # Run SAHI sliced inference directly on the NumPy array
    # NOTE: SAHI requires RGB input, so convert the BGR frame
    frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

    result = get_sliced_prediction(
        image=frame_rgb,
        detection_model=detection_model,
        slice_height=SLICE_HEIGHT,
        slice_width=SLICE_WIDTH,
        overlap_height_ratio=OVERLAP_RATIO,
        overlap_width_ratio=OVERLAP_RATIO,
        postprocess_type='NMS',
        postprocess_match_threshold=0.3
    )

    # Draw Bounding Boxes onto the Frame
    annotated_frame = frame.copy() # Start with the original BGR frame

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

    return frame_count, annotated_frame

def read_frames(input_video_path: str, frame_q: mp.Queue):
    """
    PRODUCER: Reads frames from the video and puts them into the queue.
    """
    cap = cv2.VideoCapture(input_video_path)
    if not cap.isOpened():
        print(f"Error: Could not open video file: {input_video_path}")
        return

    frame_count = 0
    while cap.isOpened():
        ret, frame = cap.read()
        if not ret:
            break
        frame_count += 1
        # Put a tuple of (frame_count, frame_np_array) into the queue
        frame_q.put((frame_count, frame))

    cap.release()
    print(f"\nReader finished. Total frames read: {frame_count}")
    # Signal the end of processing by putting 'None' markers for each worker
    for _ in range(NUM_WORKERS):
        frame_q.put(None)

def worker_wrapper(frame_q: mp.Queue, result_q: mp.Queue, model_path: str, device: str):
    """
    A persistent worker that gets frames from frame_q and puts results in result_q.
    This ensures the model is loaded only once per process.
    """
    detection_model = init_detection_model(model_path, device)
    
    while True:
        frame_data = frame_q.get()
        if frame_data is None:
            break # Exit signal
        
        frame_count, annotated_frame = process_frame(frame_data, detection_model)
        result_q.put((frame_count, annotated_frame))
        
    result_q.put(None) # Signal this worker is done to the writer

def write_frames(output_video_path: str, result_q: mp.Queue, total_frames: int, video_info: dict):
    """
    CONSUMER: Gets annotated frames from the queue and writes them in correct order.
    """
    width, height, fps = video_info['width'], video_info['height'], video_info['fps']
    
    # Setup VideoWriter
    fourcc = cv2.VideoWriter_fourcc(*'XVID') # Codec for AVI
    out = cv2.VideoWriter(output_video_path, fourcc, fps, (width, height))
    if not out.isOpened():
        print(f"Error: Could not open VideoWriter for output file: {output_video_path}")
        return

    # Use a dictionary to hold results out of order
    results_buffer = {}
    next_frame_to_write = 1
    workers_finished = 0

    while workers_finished < NUM_WORKERS:
        item = result_q.get()
        
        if item is None:
            workers_finished += 1
            continue

        frame_count, annotated_frame = item
        results_buffer[frame_count] = annotated_frame
        
        # Write frames in correct order
        while next_frame_to_write in results_buffer:
            frame_to_write = results_buffer.pop(next_frame_to_write)
            out.write(frame_to_write)
            sys.stdout.write(f"\rWriting frame {next_frame_to_write}/{total_frames}...")
            sys.stdout.flush()
            next_frame_to_write += 1

    out.release()
    print(f"\nWriter finished. Total frames written: {next_frame_to_write - 1}")


def run_yolo_sliced_video_parallel(model_path: str, input_video_path: str, output_video_path: str):
    """
    Parallel version of the video processing.
    """
    mp.set_start_method('spawn', force=True) # Recommended for CUDA/PyTorch

    # 1. Get Video Metadata
    cap = cv2.VideoCapture(input_video_path)
    if not cap.isOpened():
        print(f"Error: Could not open video file: {input_video_path}")
        return
        
    video_info = {
        'width': int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
        'height': int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
        'fps': cap.get(cv2.CAP_PROP_FPS),
        'total_frames': int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    }
    cap.release()
    total_frames = video_info['total_frames']
    print(f"Video Info: {video_info}. Using {NUM_WORKERS} worker processes.")

    # 2. Initialize Queues and Processes
    frame_q = mp.Queue(maxsize=NUM_WORKERS * 2) # Queue for raw frames (Producer -> Workers)
    result_q = mp.Queue(maxsize=NUM_WORKERS * 2) # Queue for annotated frames (Workers -> Writer)

    # Start Reader (Producer)
    reader_proc = mp.Process(target=read_frames, args=(input_video_path, frame_q))
    reader_proc.start()

    # Start Workers (Consumers/Producers)
    workers = []
    for i in range(NUM_WORKERS):
        # Use a device specific to the worker if multiple GPUs are available (e.g., 'cuda:0', 'cuda:1')
        worker_device = DEVICE if DEVICE == 'cpu' else f'cuda:{i % torch.cuda.device_count()}'
        p = mp.Process(target=worker_wrapper, 
                       args=(frame_q, result_q, model_path, worker_device))
        workers.append(p)
        p.start()

    # Start Writer (Consumer)
    writer_proc = mp.Process(target=write_frames, 
                             args=(output_video_path, result_q, total_frames, video_info))
    writer_proc.start()

    # 3. Wait for all processes to finish
    reader_proc.join()
    for p in workers:
        p.join()
    writer_proc.join()

    # 4. Cleanup
    print("\n\nVideo processing complete.")
    print(f"Annotated video saved to: {output_video_path}")


if __name__ == "__main__":
    run_yolo_sliced_video_parallel(MODEL_PATH, INPUT_VIDEO_PATH, OUTPUT_VIDEO_PATH)