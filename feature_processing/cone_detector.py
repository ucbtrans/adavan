import cv2
import numpy as np
from ultralytics import YOLO
import matplotlib.patches as patches
import matplotlib.image as mpimg
import matplotlib.pyplot as plt
from sahi import AutoDetectionModel
from sahi.predict import get_prediction
from sahi.predict import get_sliced_prediction
import os
import xml.etree.ElementTree as ET
import gpxpy
import gpxpy.gpx
import math



def get_frame_at_timestamp(video_path, timestamp):
    """
    Video path relative video path to mp4 file
    Time stamp in seconds
    """
    
    try:
        cap = cv2.VideoCapture(video_path)
        cap.set(cv2.CAP_PROP_POS_MSEC,(timestamp*1000))
        success, image = cap.read()
        print(success)
        if success:
            cv2.imwrite("dash_videos/Bancroft_Vid_2/Frame_Images/frame%d.jpg" % timestamp, image)
    except Exception as e:
        print(f"An error occurred: {e}")
        return None
    
def train_YOLO_model():
    return

def extract_coordinates(filepath):
    output = []
    try:
        with open(filepath, 'r') as file:
            for line in file:
                bounds = line.split()
                try:
                    bounds = [float(bound) for bound in bounds]
                    output.append(bounds[1:])
                except Exception:
                    print(f"Error: File in incorrect format at {filepath}")
    except FileNotFoundError:
        print(f"Error: File not found at {filepath}")
    except Exception as e:
        print(f"An error occurred: {e}")
    return output
                
def plot_Bounding_Box(image_filepath, bounds):
    img = mpimg.imread(image_filepath)
    height, width, channels = img.shape
    imgplot = plt.imshow(img)
    ax = plt.subplot()
    for pos in bounds:
        print(pos)
        bottom_left_x_norm = pos[0] - (pos[2]/2)
        bottom_left_y_norm = pos[1] - (pos[3]/2)    
        rect = patches.Rectangle((int(round(bottom_left_x_norm * width)), int(round(bottom_left_y_norm * height))), int(round(pos[2] * width)), 
                                 int(round(pos[3]*height)), linewidth=1, edgecolor='r', facecolor='none')
        ax.add_patch(rect)
    plt.show()
    
def train_YOLO_model(yaml_path):
    model = YOLO('yolo11n.pt')

    model.train(data="cone_dataset/traffic_cone.yaml", epochs=10, imgsz=640, batch=12)
    metrics = model.val()
    print(metrics)
    
def run_YOLO_frame(model_path):
    detection_model = AutoDetectionModel.from_pretrained(
        model_type="ultralytics",
        model_path=model_path,
        confidence_threshold=0.6,
        device="cpu",
    )
    #currModel = YOLO(model_path)
    bounding_boxes_frames = []
    for i in range(0, 6):
        result = get_sliced_prediction(
            f"/Users/ashwinbardhwaj/Documents/PATH/Work Zone Project/ucb-wzdx/src/dashcam_refiner/dash_videos/Bancroft_Vid_2/Frame_Images/frame{i}.jpg",
            detection_model,
            slice_height=1024,
            slice_width=1024,
            overlap_height_ratio=0.2,
            overlap_width_ratio=0.2
        )
        result.export_visuals(export_dir="runs/detect/inference")
        rename_file("runs/detect/inference", "prediction_visual.png", f"predicted_frame{i}.png")
        #results = currModel(f"/Users/ashwinbardhwaj/Documents/PATH/Work Zone Project/ucb-wzdx/src/dashcam_refiner/dash_videos/Bancroft_Vid_2/Frame_Images/frame{i}.jpg"
        #                    ,save=True, project="runs/detect", name="inference", exist_ok=True)
        #print(results)
        bounding_boxes = []
        for prediction in result.object_prediction_list:
            bbox = prediction.bbox.to_xyxy() #Convert to [x_min, y_min, x_max, y_max] format
            bounding_boxes.append(bbox)
        bounding_boxes_frames.append(bounding_boxes)
    return bounding_boxes_frames
        
def calculate_angle(box, image_path):
    if not isinstance(box, (list, tuple)) or len(box) != 4:
        return None
    
    img = cv2.imread(image_path)
    if img is None:
        print(f"Error: Could not read image at {image_path}")
        return None, None

    image_height, image_width, _ = img.shape

    x_min, y_min, x_max, y_max = box
    mid_x = round((x_min + x_max) / 2)
    mid_y = round((y_min + y_max) / 2)

    bottom_center_x = int(image_width / 2)
    bottom_center_y = int(image_height)

    vector = np.array([mid_x - bottom_center_x, mid_y - bottom_center_y])

    horizontal_vector = np.array([1, 0])

    angle_rad = np.arctan2(vector[1], vector[0])
    angle_deg = np.degrees(angle_rad)
    #import pdb; pdb.set_trace()
    cv2.arrowedLine(img, (bottom_center_x, bottom_center_y), (mid_x, mid_y), (0, 255, 0), 2)


    return angle_deg, img

def rename_file(directory, old_name, new_name):
    old_path = os.path.join(directory, old_name)
    new_path = os.path.join(directory, new_name)
    try:
        os.rename(old_path, new_path)
        print(f"File '{old_name}' successfully renamed to '{new_name}'.")
        return True
    except Exception as e:
        print(f"Error renaming file: {e}")
        return False

def get_source_coords(filepath):
    """
    frame_num starts at 1
    """
    
    res = {}
    gpx_file = open(filepath, 'r')
    
    gpx = gpxpy.parse(gpx_file)
    for waypoint in gpx.waypoints:
        print(f'waypoint {waypoint.name} -> ({waypoint.latitude},{waypoint.longitude})')
        
    for track in gpx.tracks:
        for segment in track.segments:
            i = 1
            for point in segment.points:
                res[i] = (point.latitude, point.longitude)
                print(f'Point at {point.latitude},{point.longitude}')
                i+=1
    return res

def distance_spherical_law_cosines(lat1, lon1, lat2, lon2):
    R = 6371000

    lat1_rad = math.radians(lat1)
    lon1_rad = math.radians(lon1)
    lat2_rad = math.radians(lat2)
    lon2_rad = math.radians(lon2)

    dlon = lon2_rad - lon1_rad

    a = math.sin(lat1_rad) * math.sin(lat2_rad) + math.cos(lat1_rad) * math.cos(lat2_rad) * math.cos(dlon)
    c = math.acos(a)
    distance = R * c

    return distance


def compute_object_distance(delta_x, angle_1, angle_2):
    angle_3 = 180 - angle_1 - angle_2
    sin_1 = np.sin(np.deg2rad(angle_1))
    sin_2 = np.sin(np.deg2rad(angle_2))
    sin_3 = np.sin(np.deg2rad(angle_3))

    dist_1 = (delta_x / sin_3) * sin_2
    dist_2 = (delta_x / sin_3) * sin_1
    
    return dist_1, dist_2
    
def run_cone_detection():
    bounding_boxes = run_YOLO_frame("/Users/ashwinbardhwaj/Documents/PATH/Work Zone Project/ucb-wzdx/src/dashcam_refiner/runs/detect/models/train8/weights/best.pt")
    frame_1_angle, image_with_vector_1 = calculate_angle(bounding_boxes[3][1], "/Users/ashwinbardhwaj/Documents/PATH/Work Zone Project/ucb-wzdx/src/dashcam_refiner/runs/detect/inference/predicted_frame3.png")
    
    frame_1_angle = (-1) * frame_1_angle
    
    cv2.imshow(f"Image with Vector: {frame_1_angle}", image_with_vector_1)
    cv2.waitKey(0)
    cv2.destroyAllWindows()
    
    frame_2_angle, image_with_vector_2 = calculate_angle(bounding_boxes[4][0], "/Users/ashwinbardhwaj/Documents/PATH/Work Zone Project/ucb-wzdx/src/dashcam_refiner/runs/detect/inference/predicted_frame4.png")
   
    frame_2_angle = (-1) * frame_2_angle
   
    cv2.imshow(f"Image with Vector: {frame_2_angle}", image_with_vector_2)
    cv2.waitKey(0)
    cv2.destroyAllWindows()
    
    frame_1_angle = 90 - frame_1_angle
    frame_2_angle = 90 + frame_2_angle
    
    print(f"Cone angle from frame 1: {frame_1_angle}")
    print(f"Cone angle from frame 2: {frame_2_angle}")

    
    
    coord_readings = get_source_coords("Bancroft_Videos2_Decoded.txt")
    
    frame_1_pos = coord_readings[2]
    frame_2_pos = coord_readings[3]
    
    print(f"Frame 1 vehicle position: {frame_1_pos}")
    print(f"Frame 2 vehicle position: {frame_2_pos}")
    
    delta_x = distance_spherical_law_cosines(frame_1_pos[0], frame_1_pos[1], frame_2_pos[0], frame_2_pos[1])
    
    print(f"Distacne vehicle travelled between two frames: {delta_x}")
    
    dist_1, dist_2 = compute_object_distance(delta_x, frame_1_angle, frame_2_angle)
    
    print(f"Distance of cone from the first and second frames: {dist_1}, {dist_2}")

    
if __name__ == "__main__":
    #for i in range(1, 61):
    #    get_frame_at_timestamp("dash_videos/Bancroft_Vid_2/Bancroft_Vids_2.mp4", i)
    #target_name = "0_Cross-Street-road-closure_jpg.rf.b268533f2d69236b4bda24587fe8eba8"
    #label_path = f"/Users/ashwinbardhwaj/Documents/PATH/Work Zone Project/ucb-wzdx/src/dashcam_refiner/cone_dataset/labels/train/0_Cross-Street-road-closure_jpg.rf.b268533f2d69236b4bda24587fe8eba8.txt"
    #bounds = extract_coordinates(label_path)
    
    #img_path = f"/Users/ashwinbardhwaj/Documents/PATH/Work Zone Project/ucb-wzdx/src/dashcam_refiner/cone_dataset/images/train/0_Cross-Street-road-closure_jpg.rf.b268533f2d69236b4bda24587fe8eba8.jpg"
    
    #plot_Bounding_Box(img_path, bounds)
    #train_YOLO_model("traffic_cone.yaml")
    run_cone_detection()
    
        