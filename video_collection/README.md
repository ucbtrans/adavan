# Hardware Requirements:

- Stereolabs ZED 2i
- SimpleRTK2b Budget with U-Box antenna
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

Flashing the operating system is composed of 3 steps.

1. Download the image file: https://developer.nvidia.com/downloads/embedded/l4t/r35_release_v5.0/jp513-orin-nano-sd-card-image.zip
2. Download Balena Etcher, which is an application to flash files: https://etcher.balena.io/
3. Find the image file and target device in the Balena Etcher application, and start the flash.

Once that is done, make sure to safely eject the sd card, place it in the nano, and power it on.

The operating system is based off of Ubuntu Linux, called Jetpack (version 6.2), and you can just follow the default setup. Make sure to download Chromium when it gives you the chance though.

If Chromium does not run for some reason, run the following commands below:

```commandline
snap download snapd --revision=24724
sudo snap ack snapd_24724.assert
sudo snap install snapd_24724.snap
sudo snap refresh --hold snapd
```

If that does not work, there are some alternatives on this website: https://jetsonhacks.com/2025/07/12/why-chromium-suddenly-broke-on-jetson-orin-and-how-to-bring-it-back/

Once the initial setup is complete, download the ZED sdk from this link: [https://www.stereolabs.com/developers/release/5.0#82af3640d775](https://www.stereolabs.com/developers/release/5.0#82af3640d775)

Here is the version that is used in the first prototypes though: https://download.stereolabs.com/zedsdk/5.0/l4t36.4/jetsons

In the terminal run the following commands

```commandline
chmod +x PATH
./PATH
```

Of course, replace PATH with the path of the downloaded sdk file.

Follow the instructions when running the file. Make sure to include the python API, any dependencies, and to train the models upon download. Basically, say yes when prompted to.

Download all the other packages in the requirements.txt file in this directory to make sure all the same packages are there.

The IDE used in the early computers was PyCharm Community, which can be downloaded through the "Software" app that comes with Jetpack.

From there, clone this repo into the computer, and you should be able to start developing and tinkering with the code.

# Preparing for Car Installation

In order to install it into the car, you need to automate the process. The easiest way to do this is to use crontab.

You can add crontab commands by typing this into your terminal:

```commandline
crontab -e
```





