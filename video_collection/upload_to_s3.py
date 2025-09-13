import os, subprocess
import time

filepath = "video/RV1/"
while True:
    files = os.listdir(filepath)
    for i in files:
        if i != 'upload_to_s3.sh' and os.path.getsize(filepath + i) > 0:
            # subprocess.run(['chmod -x upload_to_s3.sh'], shell=True)
            subprocess.run(['./upload_to_s3.sh', filepath+i])
    time.sleep(60)