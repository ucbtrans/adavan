from datetime import datetime, timezone, timedelta
import time
import serial
import sys
import pyzed.sl as sl
from signal import signal, SIGINT
import csv
import argparse
import os
time.sleep(15)
cam = sl.Camera()


# Handler to deal with CTRL+C properly
def handler(signal_received, frame):
    cam.disable_recording()
    cam.close()
    sys.exit(0)


signal(SIGINT, handler)

def get_current_datetime(now):
    return now.strftime("%d-%m-%Y_%H-%M-%S")


def main(opt):
    init = sl.InitParameters()
    init.depth_mode = sl.DEPTH_MODE.NONE  # Set configuration parameters for the ZED
    init.async_image_retrieval = False  # This parameter can be used to record SVO in camera FPS even if the grab loop is running at a lower FPS (due to compute for ex.)
    init.camera_fps = 15
    status = cam.open(init)

    GPS_DEVICE = "/dev/serial/by-id/usb-u-blox_AG_-_www.u-blox.com_u-blox_GNSS_receiver-if00"
    BAUDRATE = 9600

    ser = serial.Serial(GPS_DEVICE, baudrate=BAUDRATE)
    ser.flushInput()
    ser.flushOutput()
    ser.readline()

    start = datetime.now(timezone.utc)

    coords = ["Null", "Null", "Null", "Null", "Null", "Null"]
    while True:
        if status != sl.ERROR_CODE.SUCCESS:
            print("Camera Open", status, "Exit program.")
            exit(1)
        recording_param = sl.RecordingParameters(opt.output_svo_file_path +opt.vehicle_key+ '/'+ get_current_datetime(start) +".svo2",
                                                 sl.SVO_COMPRESSION_MODE.LOSSLESS)  # Enable recording with the filename specified in argument
        err = cam.enable_recording(recording_param)
        if err != sl.ERROR_CODE.SUCCESS:
            print("Recording ZED : ", err)
            exit(1)

        runtime = sl.RuntimeParameters()
        print("SVO is Recording, use Ctrl-C to stop.")  # Start recording SVO, stop with Ctrl-C command
        frames_recorded = 0
        gps_coords_list = []
        while frames_recorded < 15*60:
            if cam.grab(runtime) <= sl.ERROR_CODE.SUCCESS:  # Check that a new image is successfully acquired
                frames_recorded += 1
                print("Frame count: " + str(frames_recorded), end="\r")
            else:
                print("Camera Disconnected")
                exit(1)
            while ser.in_waiting > 0:
                nmea_sentence = str(ser.readline())
                nmea_sentence = nmea_sentence.split(",")
                if nmea_sentence[0] == "b'$GNGGA":
                    # The GNGGA Line is GNGGA, time, latitude, N/S, Longitude, E/W
                    coords = nmea_sentence[1:6]

            gps_coords_list.append({"Camera_time": cam.get_timestamp(sl.TIME_REFERENCE.CURRENT), "GPS_time": coords[0], "Latitude": coords[1],
                                    "Latitude_direction": coords[2],
                                    "Longitude": coords[3], "Longitude_direction": coords[4]})
            coords = ["Null" for i in range(5)]
        with open(opt.output_svo_file_path +opt.vehicle_key + '/'+ get_current_datetime(start) +".csv", "w", newline="") as csvfile:
            fieldnames = ["Camera_time", "GPS_time", "Latitude", "Latitude_direction", "Longitude",
                          "Longitude_direction"]
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(gps_coords_list)
        cam.disable_recording()
        start += timedelta(minutes=1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--output_svo_file_path', type=str, help='Path to the SVO files that will be written', required=True)
    parser.add_argument('--vehicle_key', type=str, help='An identifier for the vehicle the camera is on', required=True)
    opt = parser.parse_args()
    main(opt)