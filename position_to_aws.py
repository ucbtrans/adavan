import csv
import sys
import json
import boto3
import osmnx as osm

BUCKET = "ada-van-service-data"

def convert_to_decimal_degrees(raw_value):
    """
    Converts coordinates from DDDMM.MMMMM format to Decimal Degrees.
    Formula: DD + (MM.MMMM / 60)
    """
    abs_val = abs(float(raw_value))
    # Degrees (DD): Digits before the last two digits of the integer part
    degrees = int(abs_val // 100)
    # Minutes (MM.MMMM): Last two digits of the integer part plus the decimal part
    minutes = abs_val % 100
    
    decimal_degrees = degrees + (minutes / 60.0)
    return decimal_degrees

def get_street_name_from_lat_long(lat, lon, dist=100):
    # This routine now receives standard Decimal Degrees
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
            
            # Apply the new conversion formula
            # Latitude is typically North (positive)
            lat = convert_to_decimal_degrees(last_valid["Latitude"])
            
            # Longitude is West (applying negative sign as per instructions)
            lon = -convert_to_decimal_degrees(last_valid["Longitude"])
            
            street = get_street_name_from_lat_long(lat, lon, dist=100)          
        else:
            camera_time = "NULL"
            lat = "NULL"
            lon = "NULL"
            street = "NULL"
            print("No valid rows found.")

        return {"Time": camera_time, "Latitude": lat, "Longitude": lon, "Street": street}
            
    except Exception as e:
        print(f"OSM Error: {e}")
        return {"Time": "NULL", "Latitude": "NULL", "Longitude": "NULL", "Street": "Street name not available"}

def position_to_aws(csv_file):
    data = get_street(csv_file)
    print(data)
    file_name = "van_position.json"
    with open(file_name, 'w') as json_file:
        json.dump(data, json_file, indent=4)

    s3_client = boto3.client('s3')
    try:
        s3 = boto3.resource('s3')
        s3.Bucket(BUCKET).upload_file(file_name, file_name)
    except Exception as e:
        print(f"AWS Error: {e}")

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python script.py <csv_file_path>")
    else:
        position_to_aws(sys.argv[1])