# Requirements:

- Stereolabs ZED 2i
- GNSS
- Mobile Modem
- Jetson Orin Nano Developer Kit

# Setup

In order to use the Jetson Orin Nano Developer
kit, it is necessary to have a micro SD card with
high speed read/write capability. My recommendation
is the Samsung Pro Ultimate 128 GB.

First thing necessary is to the flash the operating system
onto the micro SD card. This needs another computer that can
take a micro SD card, or you have an adapter.

Follow the instructions on this link to flash the operating system: [https://www.jetson-ai-lab.com/initial_setup_jon.html](https://www.jetson-ai-lab.com/initial_setup_jon.html)

Make sure to safely eject the sd card, place it in the nano, and power it on.

The operating system is based off of Ubuntu Linux, and you can just follow the default setup.

Once the initial setup is complete, download the ZED sdk from this link: [https://www.stereolabs.com/developers/release/5.0#82af3640d775](https://www.stereolabs.com/developers/release/5.0#82af3640d775)

Follow the instructions when running the file. Make sure to include the python API, any dependencies, and to train the models upon download.

From there, clone this repo into the computer, and 