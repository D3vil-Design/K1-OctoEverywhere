
from octoeverywhere.commandhandler import CommandHandler, CommandResponse

from .moonrakerclient import MoonrakerClient
from .smartpause import SmartPause

# This class implements the Platform Command Handler Interface
class MoonrakerCommandHandler:


    def __init__(self, logger) -> None:
        self.Logger = logger


    # !! Platform Command Handler Interface Function !!
    #
    # This must return the common "JobStatus" dict or None on failure.
    # The format of this must stay consistent with OctoPrint and the service.
    # Returning None send back the NoHostConnected error, assuming that the plugin isn't connected to the host or the host isn't
    # connected to the printer's firmware.
    #
    # See the JobStatusV2 class in the service for the object definition.
    #
    def GetCurrentJobStatus(self):
        result = MoonrakerClient.Get().SendJsonRpcRequest("printer.objects.query",
        {
            "objects": {
                "print_stats": None,    # Needed for many things, including GetPrintTimeRemainingEstimateInSeconds_WithPrintStatsAndVirtualSdCardResult
                "gcode_move": None,     # Needed for GetPrintTimeRemainingEstimateInSeconds_WithPrintStatsAndVirtualSdCardResult to get the current speed
                "virtual_sdcard": None, # Needed for many things, including GetPrintTimeRemainingEstimateInSeconds_WithPrintStatsAndVirtualSdCardResult
                # "webhooks": None,
                # "toolhead": None,
                # "extruder": None,
                # "bed_mesh": None,

            }
        })
        # Validate
        if result.HasError():
            self.Logger.error("MoonrakerCommandHandler failed GetCurrentJobStatus() query. "+result.GetLoggingErrorStr())
            return None

        # Get the result.
        res = result.GetResult()

        # Map the state
        state = "idle"
        if "status" in res and "print_stats" in res["status"] and "state" in res["status"]["print_stats"]:
            # https://moonraker.readthedocs.io/en/latest/printer_objects/#print_stats
            mrState = res["status"]["print_stats"]["state"]
            if mrState == "standby":
                state = "idle"
            elif mrState == "printing":
                # This is a special case, we consider "warmingup" a subset of printing.
                if MoonrakerClient.Get().GetMoonrakerCompat().CheckIfPrinterIsWarmingUp_WithPrintStats(result):
                    state = "warmingup"
                else:
                    state = "printing"
            elif mrState == "paused":
                state = "paused"
            elif mrState == "complete":
                state = "complete"
            elif mrState == "cancelled":
                state = "cancelled"
            elif mrState == "error":
                state = "error"
            else:
                self.Logger.warn("Unknown mrState returned from print_stats: "+str(mrState))
        else:
            self.Logger.warn("MoonrakerCommandHandler failed to find the print_stats.status")

        # TODO - If in an error state, set some context as to why.
        errorStr_CanBeNone = None

        # Get duration and filename.
        durationSec = 0
        fileName = ""
        if "status" in res and "print_stats" in res["status"]:
            ps = res["status"]["print_stats"]
            # We choose to use print_duration over "total_duration" so we only show the time actually spent printing. This is consistent across platforms.
            if "print_duration" in ps:
                durationSec = int(ps["print_duration"])
            if "filename" in ps:
                fileName = ps["filename"]

        # Get the progress
        progress = 0.0
        if "status" in res and "virtual_sdcard" in res["status"]:
            vs = res["status"]["virtual_sdcard"]
            if "progress" in vs:
                # Convert progress 0->1 to 0->100
                progress = vs["progress"] * 100.0


        # Time left can be hard to compute correctly, so use the common function to do it based
        # on what we can get as a best effort.
        timeLeftSec = MoonrakerClient.Get().GetMoonrakerCompat().GetPrintTimeRemainingEstimateInSeconds_WithPrintStatsVirtualSdCardAndGcodeMoveResult(result)

        # Build the object and return.
        return {
            "State": state,
            "Error": errorStr_CanBeNone,
            "CurrentPrint":
            {
                "Progress" : progress,
                "DurationSec" : durationSec,
                "TimeLeftSec" : timeLeftSec,
                "FileName" : fileName
            }
        }


    # !! Platform Command Handler Interface Function !!
    # This must return the platform version as a string.
    def GetPlatformVersionStr(self):
        # We don't supply this for moonraker at the moment.
        return "1.0.0"


    # !! Platform Command Handler Interface Function !!
    # This must return a CommandResponse.
    def ExecutePause(self, smartPause, suppressNotificationBool, disableHotendBool, disableBedBool, zLiftMm, retractFilamentMm, showSmartPausePopup):

        # Only allow the pause if the print state is printing, otherwise the system seems to get confused.
        result = MoonrakerClient.Get().SendJsonRpcRequest("printer.objects.query",
        {
            "objects": {
                "print_stats": None
            }
        })
        if result.HasError():
            self.Logger.error("ExecuteSmartPause failed to query printer status. "+result.GetLoggingErrorStr())
            return CommandResponse.Error(500, "failed to get printer state.")
        res = result.GetResult()
        if "status" not in res or "print_stats" not in res["status"] or "state" not in res["status"]["print_stats"]:
            self.Logger.error("ExecuteSmartPause failed to get the current state.")
            return CommandResponse.Error(500, "Failed to find state.")
        state = res["status"]["print_stats"]["state"]

        # This is the only state we allow pause.
        if state != "printing":
            self.Logger.warn("SmartPause failed because the state is not printing. "+str(state))
            return CommandResponse.Error(CommandHandler.c_CommandError_NoPrintRunning, "Printer state is not printing.")

        # The smart pause logic handles all pause commands.
        return SmartPause.Get().ExecuteSmartPause(suppressNotificationBool)