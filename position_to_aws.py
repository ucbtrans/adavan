import csv
import sys
import json
import boto3
import osmnx as osm


BUCKET = "ada-van-service-data"


def get_street_name_from_lat_long(lat, lon, dist=10000):
    # Create a point and a search buffer (dist in meters)
    try:
        graph = osm.graph.graph_from_point((lat, lon), dist=dist, network_type='drive')
        nearest_edge = osm.distance.nearest_edges(graph, lon, lat)

        u, v, k = nearest_edge
        street_name = graph.get_edge_data(u, v, k)['name']

        return street_name
    except Exception as e:
        return f"Street not found: {e}"


def get_street(file_path):
    last_valid = None

    try:
        with open(file_path, newline='', encoding="utf-8") as csvfile:
            reader = csv.DictReader(csvfile)
            
            for row in reader:
                lat = row.get("Latitude")
                lon = row.get("Longitude")
            
                if lat and lon and lat.upper() != "NULL" and lon.upper() != "NULL":
                    last_valid = row
        
        if last_valid:
            camera_time = last_valid["Camera_time"]
            lat = float(last_valid["Latitude"])/100.0
            lon = float(last_valid["Longitude"])/-100.0
            street = get_street_name_from_lat_long(lat, lon, dist=10000)          
        else:
            camera_time = "NULL"
            lat = "NULL"
            lon = "NULL"
            street = "NULL"
            print("No valid rows found.")

        return {"Time": camera_time, "Latitude": lat, "Longitude": lon, "Street": street}
            
    except Exception as e:
        print(e)
        return {"Time": "NULL", "Latitude": "NULL", "Longitude": "NULL", "Street": "Street name not available"}


def position_to_aws(csv_file):

    data = get_street(csv_file)

    file_name = "van_position.json"
    with open(file_name, 'w') as json_file:
        json.dump(data, json_file, indent=4)

    s3_client = boto3.client('s3')

    try:
        s3 = boto3.resource('s3')
        s3.Bucket(BUCKET).upload_file(file_name, file_name)
    except ClientError as e:
        print(f"Error: {e}")


if __name__ == "__main__":

    if len(sys.argv) != 2:
        print("Usage: python script.py <csv_file_path>")
    else:
        position_to_aws(sys.argv[1])
