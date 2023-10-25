[![ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/X8X7LBLK2)

# K1 Octoeverywhere

This repo edits the install script for Octoeverywhere to work with the limited MIPS architecture for the Creality K1. There are various services not available such as systemd, pushd, apt, etc. There are additional steps you will need to take as well.

## Requirements

This requires you to have root access to your Creality K1. This is done through an exploit using a shadow gcode file. For more information, please check out https://github.com/giveen/K1_Files/tree/ab81d83ca6421c8420a7a85e456059eb0e641bd3/exploit

## Caveats

Firmware updates will most likely completely overwrite these changes.

## Installation

```sh
sed -i 's/#!\/usr\/bin\/python$/#!\/usr\/bin\/python3/g' /usr/bin/virtualenv
mkdir -p /usr/data/octoeverywhere-logs
mkdir -p /etc/systemd/system/
echo "Environment=MOONRAKER_CONF=/usr/data/printer_data/config/moonraker.conf" > /etc/systemd/system/moonraker.service
cd /usr/data
git clone https://github.com/D3vil-Design/K1-OctoEverywhere.git octoeverywhere
cd octoeverywhere
./install.sh
```
The script will hang on `Waiting for the plugin to produce a printer id...` - go ahead and respond `n` when it asks you if you want to keep waiting.
```sh
cp startup_script.sh /usr/data/startup_script.sh
chmod +x /usr/data/startup_script.sh
/usr/data/startup_script.sh
```
Click the link that the script echos to finish setup on Octoeverywhere's website
After your Browser tells you "Secure Printer Link Established", close the tab and CTRL + C couple of times in console to exit the startup_script.sh
```sh
cp S99octoeverywhere /etc/init.d/S99octoeverywhere
chmod +x /etc/init.d/S99octoeverywhere
/etc/init.d/S99octoeverywhere restart
```
Profit, enjoy! :)
