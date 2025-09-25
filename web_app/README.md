# Requirements To Run on Boot-Up

On the Jetson Orin Nano, run the following command.

sudo nano /etc/systemd/system/flask-app.service

In this file, write the following.

[Unit]
Description=Flask Web Application
After=network.target

[Service]
User= Jetson Username
WorkingDirectory=/path/to/your/project_directory
ExecStart=/usr/bin/python3 /path/to/your/project_directory/app.py
Restart=always

[Install]
WantedBy=multi-user.target

Make sure to change the User, WorkingDirectory, and ExecStart.

Run the following commands

sudo systemctl daemon-reload

sudo systemctl enable flask-app.service

sudo systemctl start flask-app.service

Check the status by:

sudo systemctl status flask-app.service

# Finding the Web App on a Mobile Device
Run ifconfig on the Jetson and procure the IPv4 address which should hopefully be static to this machine and router.

You should be able to go to http://(IP Address):5000 to open up the app.
