#~/bin/sh

#
# The responsibility of this script is to bootstrap the setup by installing the required system libs,
# virtual environment, and py requirements. The core of the setup logic is done by the PY install script.
#

# Set this to terminate on error.
set -e

# Get the root path of the repo, aka, where this script is executing
OE_REPO_DIR=$(realpath $(dirname "$0"))

# This is the root of where our py virtual env will be. Note that all OctoEverywhere instances share this same
# virtual environment. This how the rest of the system is, where all other services, even with multiple instances, share the same
# virtual environment. I probably wouldn't have done it like this, but we have to create this before we know what instance we are targeting, so it's fine.
OE_ENV="/usr/data/octoeverywhere-env"

# Note that this is parsed by the update process to find and update required system packages on update!
# On update THIS SCRIPT ISN'T RAN, only this line is parsed out and used to install / update system packages.
# For python packages, the `requirements.txt` package is used on update.
# This var name MUST BE `PKGLIST`!!
PKGLIST="python3 python3-pip virtualenv"

#
# Console Write Helpers
#
c_default=$(echo -en "\e[39m")
c_green=$(echo -en "\e[92m")
c_yellow=$(echo -en "\e[93m")
c_magenta=$(echo -en "\e[35m")
c_red=$(echo -en "\e[91m")
c_cyan=$(echo -en "\e[96m")

log_header()
{
    echo -e "${c_magenta}$1${c_default}"
}

log_important()
{
    echo -e "${c_yellow}$1${c_default}"
}

log_error()
{
    log_blank
    echo -e "${c_red}$1${c_default}"
    log_blank
}

log_info()
{
    echo -e "${c_green}$1${c_default}"
}

log_blank()
{
    echo ""
}

#
# Logic to create / update our virtual py env
#
ensure_py_venv()
{
    log_header "Checking Python Virtual Environment For OctoEverywhere..."
    # If the service is already running, we can't recreate the virtual env
    # so if it exists, don't try to create it.
    if [ -d $OE_ENV ]; then
        # This virtual env refresh fails on some devices when the service is already running, so skip it for now.
        # This only refreshes the virtual environment package anyways, so it's not super needed.
        #log_info "Virtual environment found, updating to the latest version of python."
        #python3 -m venv --upgrade "${OE_ENV}"
        return 0
    fi

    log_info "No virtual environment found, creating one now."
    mkdir -p "${OE_ENV}"
    virtualenv -p /usr/bin/python3 --system-site-packages "${OE_ENV}"
}

#
# Logic to make sure all of our required system and PY libs are installed
#
install_or_update_dependencies()
{
    log_header "Checking required system packages are installed..."
    log_important "You might be asked for your system password - this is required to install the required system packages."

    # Now, ensure the virtual environment is created.
    ensure_py_venv

    # Finally, ensure our plugin requirements are installed and updated.
    log_info "Installing or updating required python libs..."
    "${OE_ENV}"/bin/pip3 install -q -r "${OE_REPO_DIR}"/requirements.txt
    log_info "Python libs installed."
}

#
# Logic to ensure the user isn't trying to use this script to setup in OctoPrint.
#
check_for_octoprint()
{
    # Do a basic check to see if OctoPrint is running on the standard port.
    # This obviously doesn't work for all OctoPrint setups, but it works for the default ones.
    if curl -s "http://127.0.0.1:5000" >/dev/null ; then
        log_important "Just a second... OctoPrint was detected!"
        log_blank
        log_important "This install script is used to install OctoEverywhere for Mainsail, Fluidd, Moonraker, etc."
        log_important "If you want to install OctoEverywhere for OctoPrint, you need to use OctoPrint's Plugin Manager, found in OctoPrint's web settings UI."
        log_blank
        read -p       "Do you want to continue this setup for Mainsail, Fluidd, Moonraker, etc? [y/n]: " -e result
        log_blank
        if [ "${result^^}" != "Y" ] ; then
            log_info "Stopping install process."
            exit 0
        fi
    fi
}

log_blank
log_blank
log_blank
cat << EOF
@@@@@@@@@@@@@@@@@@@@@@@@***@@@@@@@@@@@@@@@@@@@@@@@
@@@@@@@@@@@@@@***********************@@@@@@@@@@@@@
@@@@@@@@@@*******************************@@@@@@@@@
@@@@@@@@***********************************@@@@@@@
@@@@@,,,************************/////////*****@@@@
@@@@,,,,,,*****************//////////////******@@@
@@,,,,,,,,,,***********//////////////////*******@@
@@,,,,,,,,,,,,*******////////****///////*********@
@,,,,,,,,,,,/////////////////****//////***********
@,,,,,,,//////////////////////////////************
,,,,,,,,////////////////////////////**************
@,,,,,,,,,,,,/////////////////////****************
@,,,,,,,,,,,,,,/////////////////******************
@@,,,,,,,,,,,,,,,,//////////////*****************@
@@@,,,,,/#######,,,,///////////*****************@@
@@@@,,,##########,,,,,,,//////,****************@@@
@@@@@,##########,,,,,,,,,////,,,,*************@@@@
@@@@@########,,,,,,,,,,,,//,,,,,,,,********@@@@@@@
@@@@@#@@@@,,,,,,,,,,,,,,,,,,,,,,,,,,,***,@@@@@@@@@
@@@@@@@@@@@@@@@,,,,,,,,,,,,,,,,,,,,,@@@@@@@@@@@@@@
EOF
log_blank
log_header "           OctoEverywhere For Klipper"
log_blank
log_blank
log_important "OctoEverywhere empowers the worldwide maker community with..."
log_info      "  - Free & Unlimited Mainsail and Fluidd remote access"
log_info      "  - Free & Unlimited Next-gen AI print failure detection"
log_info      "  - Real-time print notifications"
log_info      "  - And so much more"
log_blank
log_blank

# Before we do anything, check that OctoPrint isn't found. If it is, we want to check with the user to make sure they are
# not trying to setup OE for OctoPrint.
check_for_octoprint

# The first thing we need to do is install or updated packages and ensure our virtual environment is setup.
# Since we need to make sure PY is installed, then create the virtual env, then update the PY libs, all of this
# is handled by one function.
install_or_update_dependencies

# Clean up any old bite code that might exist from the past, before we added the -B argument to the installer.
# TODO - Remove at some point.
rm -fr "${OE_REPO_DIR}/moonraker_installer/__pycache__/"

# Before launching our PY script, set any vars it needs to know
# Pass all of the command line args, so they can be handled by the PY script.
USERNAME=${USER}
USER_HOME=${HOME}
CMD_LINE_ARGS=${@}
PY_LAUNCH_JSON="{\"OE_REPO_DIR\":\"${OE_REPO_DIR}\",\"OE_ENV\":\"${OE_ENV}\",\"USERNAME\":\"${USERNAME}\",\"USER_HOME\":\"${USER_HOME}\",\"CMD_LINE_ARGS\":\"${CMD_LINE_ARGS}\"}"

log_info "Bootstrap done. Starting python installer..."
# Now launch into our py setup script, that does everything else required.
# Since we use a module for file includes, we need to set the path to the root of the module
# so python will find it.
export PYTHONPATH="${OE_REPO_DIR}"
#pushd ${OE_REPO_DIR} > /dev/null

# Disable the PY cache files (-B), since they will be written as sudo, since that's what we launch the PY
# installer as. The PY installer must be sudo to write the service files, but we don't want the
# complied files to stay in the repo with sudo permissions.
${OE_ENV}/bin/python3 -B -m moonraker_installer ${PY_LAUNCH_JSON}
#popd > /dev/null

# Check the output of the py script.
retVal=$?
if [ $retVal -ne 0 ]; then
    log_error "Failed to complete setup. Error Code: ${retVal}"
fi

# Note the rest of the user flow (and terminal info) is done by the PY script, so we don't need to report anything else.
exit $retVal