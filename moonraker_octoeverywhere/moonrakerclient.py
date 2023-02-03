import os
import threading
import time
import json
import queue

import configparser

from octoeverywhere.sentry import Sentry
from octoeverywhere.websocketimpl import Client
from octoeverywhere.notificationshandler import NotificationsHandler

# The response from a json rpc request.
class JsonRpcResponse:

    # Our specific errors
    OE_ERROR_WS_NOT_CONNECTED = 99990001
    OE_ERROR_TIMEOUT = 99990002
    OE_ERROR_EXCEPTION = 99990003

    def __init__(self, ResultObj, ErrorCode = 0, ErrorStr = None) -> None:
        self.Result = ResultObj
        self.ErrorCode = ErrorCode
        self.ErrorStr = ErrorStr
        if ErrorCode == JsonRpcResponse.OE_ERROR_TIMEOUT:
            ErrorStr = "Timeout waiting for RPC response."
        if ErrorCode == JsonRpcResponse.OE_ERROR_WS_NOT_CONNECTED:
            ErrorStr = "No active websocket connected."

    def HasError(self) -> bool:
        return self.ErrorCode != 0

    def GetErrorCode(self):
        return self.ErrorCode

    def GetErrorStr(self):
        return self.ErrorStr

    def GetLoggingErrorStr(self):
        return str(self.ErrorCode) + " - " + str(self.ErrorStr)

    def GetResult(self):
        return self.Result


# This class is our main interface to interact with moonraker. This includes the logic to make
# http requests with moonraker, as well as logic to maintain a websocket connection for requests
# and notifications.
class MoonrakerClient:

    # The max amount of time we will wait for a request before we timeout.
    RequestTimeoutSec = 30.0

    # Logic for a static singleton
    _Instance = None


    @staticmethod
    def Init(logger, moonrakerConfigFilePath, printerId):
        MoonrakerClient._Instance = MoonrakerClient(logger, moonrakerConfigFilePath, printerId)


    @staticmethod
    def Get():
        return MoonrakerClient._Instance


    def __init__(self, logger, moonrakerConfigFilePath, printerId) -> None:
        self.Logger = logger
        self.MoonrakerConfigFilePath = moonrakerConfigFilePath
        self.MoonrakerHostAndPort = "127.0.0.1:7125"

        # Setup the json-rpc vars
        self.JsonRpcIdLock = threading.Lock()
        self.JsonRpcIdCounter = 0
        self.JsonRpcWaitingContexts = {}

        # Setup the Moonraker compat helper object.
        self.MoonrakerCompat = MoonrakerCompat(self.Logger, printerId)

        # Setup the non response message thread
        # See _NonResponseMsgQueueWorker to why this is needed.
        self.NonResponseMsgQueue = queue.Queue(20000)
        self.NonResponseMsgThread = threading.Thread(target=self._NonResponseMsgQueueWorker)
        self.NonResponseMsgThread.start()

        # Setup the WS vars and a websocket worker thread.
        # Don't run it until StartRunningIfNotAlready is called!
        self.WebSocket = None
        self.WebSocketConnected = False
        self.WebSocketKlippyReady = False
        self.WebSocketLock = threading.Lock()
        self.WsThread = threading.Thread(target=self._WebSocketWorkerThread)
        self.WsThreadRunning = False
        self.WsThread.daemon = True


    # Actually starts the client running, trying to connect the websocket and such.
    # This is done after the first connection to OctoEverywhere has been established, to ensure
    # the connection is setup before this, incase something needs to use it.
    def StartRunningIfNotAlready(self, octoKey):
        # Always update the octokey, to make sure we are current.
        self.MoonrakerCompat.SetOctoKey(octoKey)

        # Only start the WS thread if it's not already running
        if self.WsThreadRunning is False:
            self.WsThreadRunning = True
            self.WsThread.start()


    # Checks to moonraker config for the host and port. We use the moonraker config so we don't duplicate the
    # value in our settings, which could change. This is called by the Websocket and then the result is saved in the class
    # This is so every http call doesn't have to read the file, but as long as the WS is connected, we know the address is correct.
    def _UpdateMoonrakerHostAndPort(self) -> None:
        # Ensure we have a file.
        if os.path.exists(self.MoonrakerConfigFilePath) is False:
            self.Logger.error("Moonraker client failed to find a moonraker config. Re-run the ./install.sh script from the OctoEverywhere repo to update the path.")
            raise Exception("No config file found")

        # Parse the config to find the host and port.
        moonrakerConfig = configparser.ConfigParser()
        moonrakerConfig.read(self.MoonrakerConfigFilePath)
        moonrakerHost = moonrakerConfig['server']['host']
        moonrakerPort = moonrakerConfig['server']['port']

        # Set the new address
        self.MoonrakerHostAndPort =  moonrakerHost + ":" + moonrakerPort


    #
    # Below this is websocket logic.
    #

    # Sends a rpc request via the connected websocket. This request will block until a response is received or the request times out.
    # This will not throw, it will always return a JsonRpcResponse which can be checked for errors or success.
    #
    # Here are the docs on the WS and JSON-RPC
    # https://moonraker.readthedocs.io/en/latest/web_api/#json-rpc-api-overview
    # https://moonraker.readthedocs.io/en/latest/web_api/#websocket-setup
    #
    # forceSendIgnoreWsState is only used to send the initial messages before the system is ready.
    def SendJsonRpcRequest(self, method, paramsDict = None, forceSendIgnoreWsState = False) -> JsonRpcResponse:
        msgId = 0
        waitContext = None
        with self.JsonRpcIdLock:
            # Get our unique ID
            msgId = self.JsonRpcIdCounter
            self.JsonRpcIdCounter += 1

            # Add our waiting context.
            waitContext = JsonRpcWaitingContext(msgId)
            self.JsonRpcWaitingContexts[msgId] = waitContext

        # From now on, we need to always make sure to clean up the wait context, even in error.
        try:
            # Create the request object
            obj = {
                "jsonrpc": "2.0",
                "method": method,
                "id": msgId
            }
            # Add the params, if there are any.
            if paramsDict is not None:
                obj["params"] = paramsDict

            # Try to send. default=str makes the json dump use the str function if it fails to serialize something.
            jsonStr = json.dumps(obj, default=str)
            self.Logger.debug("Moonraker RPC Request - "+str(id)+" : "+method+" "+jsonStr)
            if self._WebSocketSend(jsonStr, forceSendIgnoreWsState) is False:
                self.Logger.info("Moonraker client failed to send JsonRPC request "+method)
                return JsonRpcResponse(None, JsonRpcResponse.OE_ERROR_WS_NOT_CONNECTED)

            # Wait for a response
            waitContext.GetEvent().wait(MoonrakerClient.RequestTimeoutSec)

            # Check if we got a result.
            result = waitContext.GetResult()
            if result is None:
                self.Logger.info("Moonraker client timeout while waiting for request. "+str(id)+" "+method)
                return JsonRpcResponse(None, JsonRpcResponse.OE_ERROR_TIMEOUT)

            # Check for an error if found, return the error state.
            if "error" in result:
                # Get the error parts
                errorCode = JsonRpcResponse.OE_ERROR_EXCEPTION
                errorStr = "Unknown"
                if "code" in result["error"]:
                    errorCode = result["error"]["code"]
                if "message" in result["error"]:
                    errorStr = result["error"]["message"]
                return JsonRpcResponse(None, errorCode, errorStr)

            # If there's a result, return the entire response
            if "result" in result:
                return JsonRpcResponse(result["result"])

            # Finally, both are missing?
            self.Logger.error("Moonraker client json rpc got a response that didn't have an error or result object? "+json.dumps(result))
            return JsonRpcResponse(None, JsonRpcResponse.OE_ERROR_EXCEPTION, "No result or error object")

        except Exception as e:
            Sentry.Exception("Moonraker client json rpc request failed to send.", e)
            return JsonRpcResponse(None, JsonRpcResponse.OE_ERROR_EXCEPTION, str(e))

        finally:
            # Before leaving, always clean up any waiting contexts.
            with self.JsonRpcIdLock:
                if msgId in self.JsonRpcWaitingContexts:
                    del self.JsonRpcWaitingContexts[msgId]


    # Sends a string to the connected websocket.
    # forceSend is used to send the initial messages before the system is ready.
    def _WebSocketSend(self, jsonStr, forceSend = False) -> bool:
        # Only allow one send at a time, thus we do it under lock.
        with self.WebSocketLock:
            if self.WebSocketConnected is False and forceSend is False:
                self.Logger.info("Moonraker client - tired to send a websocket message when the socket wasn't open.")
                return False
            if self.WebSocketKlippyReady is False and forceSend is False:
                self.Logger.info("Moonraker client - tired to send a websocket message when the klippy init sequence wasn't done yet.")
                return False
            localWs = self.WebSocket
            if localWs is None:
                self.Logger.info("Moonraker client - tired to send a websocket message before the websocket was created.")
                return False

            # Send under lock.
            try:
                localWs.Send(jsonStr, False)
            except Exception as e:
                Sentry.Exception("Moonraker client exception in websocket send.", e)
                return False
        return True


    # Called when a new websocket is connected and klippy is ready.
    # At this point, we should setup anything we need to do and sync any state required.
    # This is called on a background thread, so we can block this.
    def _OnWsOpenAndKlippyReady(self):
        self.Logger.info("Moonraker client setting up default notification hooks")
        # First, we need to setup our notification subs
        # https://moonraker.readthedocs.io/en/latest/web_api/#subscribe-to-printer-object-status
        # https://moonraker.readthedocs.io/en/latest/printer_objects/
        #result = self.SendJsonRpcRequest("printer.objects.list")
        result = self.SendJsonRpcRequest("printer.objects.subscribe",
        {
            "objects":
            {
                # Using None allows us to get all of the data from the notification types.
                # For some types, using None has way too many updates, so we filter them down.
                "print_stats": { "state", "filename", "message" },
                "webhooks": None,
                "virtual_sdcard": None,
                "history" : None,
            }
        })

        # Verify success.
        if result.HasError():
            self.Logger.error("Failed to setup moonraker notification subs. "+result.GetLoggingErrorStr())
            self._RestartWebsocket()
            return

        # Call the event handler
        self.MoonrakerCompat.OnMoonrakerClientConnected()


    # Called when the websocket gets any other message that's not a RPC response.
    # If we throw from here, the websocket will close and restart.
    def _OnWsNonResponseMessage(self, msg):
        # Get the common method string
        if "method" not in msg:
            self.Logger.warn("Moonraker WS message received with no method "+json.dumps(msg))
            return
        method = msg["method"].lower()

        # if method != "notify_proc_stat_update":
        #     self.Logger.info("WS MSG: "+json.dumps(msg))

        # These objects can come in all shapes and sizes. So we only look for exactly what we need, if we don't find it
        # We ignore the object, someone else might match it.

        # When a print starts, we get a "notify_status_update" - "state": "printing" which is ambiguous with resume.
        # But we also get "notify_history_changed" - "action": "added" with the file name, so we use that.
        if method == "notify_history_changed":
            actionContainerObj = self._GetWsMsgParam(msg, "action")
            if actionContainerObj is not None:
                action = actionContainerObj["action"]
                if action == "added":
                    jobContainerObj = self._GetWsMsgParam(msg, "job")
                    if jobContainerObj is not None:
                        jobObj = jobContainerObj["job"]
                        if "filename" in jobObj:
                            fileName = jobObj["filename"]
                            self.MoonrakerCompat.OnPrintStart(fileName)
                            return

        if method == "notify_status_update":
            # This is shared by a few things, so get it once.
            progressFloat_CanBeNone = self._GetProgressFromMsg(msg)

            # Check for a state container
            stateContainerObj = self._GetWsMsgParam(msg, "print_stats")
            if stateContainerObj is not None:
                ps = stateContainerObj["print_stats"]
                if "state" in ps:
                    state = ps["state"]
                    # Check for pause
                    if state == "paused":
                        self.MoonrakerCompat.OnPrintPaused()
                        return
                    # Resume is hard, because it's hard to tell the difference between printing we get from the starting message
                    # and printing we get from a resume. So the way we do it is by looking at the progress, to see if it's just starting or not.
                    elif state == "printing":
                        if progressFloat_CanBeNone is None or progressFloat_CanBeNone > 0.0001:
                            self.MoonrakerCompat.OnPrintResumed()
                            return

            # Report progress. Do this after the others so they will report before a potential progress update.
            # Progress updates super frequently (like once a second) so there's plenty of chances.
            if progressFloat_CanBeNone is not None:
                self.MoonrakerCompat.OnPrintProgress(progressFloat_CanBeNone)


    # If the message has a progress contained in the virtual_sdcard, this returns it. The progress is a float from 0.0->1.0
    # Otherwise None
    def _GetProgressFromMsg(self, msg):
        vsdContainerObj = self._GetWsMsgParam(msg, "virtual_sdcard")
        if vsdContainerObj is not None:
            vsd = vsdContainerObj["virtual_sdcard"]
            if "progress" in vsd:
                return vsd["progress"]
        return None


    # Given a property name, returns the correct param object that contains that object.
    def _GetWsMsgParam(self, msg, paramName):
        if "params" not in msg:
            return None
        paramArray = msg["params"]
        for p in paramArray:
            # Only test things that are dicts.
            if isinstance(p, dict) and paramName in p:
                return p
        return None


    def _WebSocketWorkerThread(self):
        self.Logger.info("Moonraker client starting websocket connection thread.")
        while True:
            try:
                # Every time we connect, call the function to update the host and port if required.
                # We only call this from the WS and cache result, so every http call doesn't need to do it.
                # We know if the WS is connected, the host and port must be correct.
                self._UpdateMoonrakerHostAndPort()

                # Create a websocket client and start it connecting.
                url = "ws://"+self.MoonrakerHostAndPort+"/websocket"
                self.Logger.info("Connecting to moonraker: "+url)
                with self.WebSocketLock:
                    self.WebSocket = Client(url,
                                    self._OnWsOpened,
                                    self._onWsMsg,
                                    None, # self._onWsData, all messages are passed to data and msg, so we don't need this.
                                    self._onWsClose,
                                    self._onWsError
                                    )

                # Run until the socket closes
                self.WebSocket.RunUntilClosed()

            except Exception as e:
                Sentry.Exception("Moonraker client exception in main WS loop.", e)

            # Inform that we lost the connection.
            self.Logger.info("Moonraker client websocket connection lost. We will try to restart it soon.")

            # Set that the websocket is disconnected.
            with self.WebSocketLock:
                self.WebSocketConnected = False
                self.WebSocketKlippyReady = False

            # This will only happen if the websocket closes or there was an error.
            # Sleep for a bit so we don't spam the system with attempts.
            time.sleep(5.0)


    # Based on the docs: https://moonraker.readthedocs.io/en/latest/web_api/#websocket-setup
    # After the websocket is open, we need to do this sequence to make sure the system is healthy and ready.
    def _AfterOpenReadyWaiter(self, targetWsObjRef):

        logCounter = 0
        self.Logger.info("Moonraker client waiting for klippy ready...")
        while True:
            try:
                # Ensure we are still using the active websocket. We use this to know if the websocket we are
                # trying to monitor is gone and the system has started a new one.
                testWs = self.WebSocket
                if testWs is None or testWs is not targetWsObjRef:
                    self.Logger.warn("The target websocket changed while waiting on klippy ready.")
                    return

                # Query the state, use the force flag to make sure we send even though klippy ready is not set.
                result = self.SendJsonRpcRequest("server.info", None, True)

                # Check for error
                if result.HasError():
                    self.Logger.error("Moonraker client failed to send klippy ready query message. "+result.GetLoggingErrorStr())
                    raise Exception("Error returned from klippy state query. "+ str(result.GetLoggingErrorStr()))

                # Check for klippy state
                resultObj = result.GetResult()
                if "klippy_state" not in resultObj:
                    self.Logger.error("Moonraker client got a klippy ready query response, but there was no klippy_state? "+json.dumps(resultObj))
                    raise Exception("No klippy_state found in result object. "+ str(result.GetLoggingErrorStr()))

                # Handle klippy state
                state = resultObj["klippy_state"]
                if state == "ready":
                    # Ready
                    self.Logger.info("Moonraker client klippy state is ready. Moonraker connection is ready and stable.")
                    with self.WebSocketLock:
                        self.WebSocketKlippyReady = True
                    # Call the connected and ready function, to let anything else do a new connection setup.
                    self._OnWsOpenAndKlippyReady()
                    # Done
                    return

                if state == "startup" or state == "error" or state == "shutdown":
                    logCounter += 1
                    # 2 seconds * 150 = one log every 5 minutes. We don't want to log a ton if the printer is offline for a long time.
                    if logCounter % 150 == 1:
                        self.Logger.info("Moonraker client got klippy state '"+state+"', waiting for ready...")
                    # We need to wait until ready. The doc suggest we wait 2 seconds.
                    time.sleep(2.0)
                    continue

                # Unknown state
                self.Logger.error("Moonraker client is in an unknown klippy waiting state. "+state)
                raise Exception("Unknown klippy waiting state.")

            except Exception as e:
                Sentry.Exception("Moonraker client exception in klippy waiting logic.", e)
                # Shut down the websocket so we do the reconnect logic.
                self._RestartWebsocket()


    # Kills the current websocket connection. Our logic will auto try to reconnect.
    def _RestartWebsocket(self):
        with self.WebSocketLock:
            if self.WebSocket is None:
                return
            self.Logger.info("Moonraker client websocket shutdown called.")
            self.WebSocket.Close()


    # Called when the websocket is opened.
    def _OnWsOpened(self, ws):
        self.Logger.info("Moonraker client websocket opened.")

        # Set that the websocket is open.
        with self.WebSocketLock:
            self.WebSocketConnected = True

        # According to the docs, there's a startup sequence we need to before sending requests.
        # We use a new thread to do the startup sequence, since we can't block this or we won't get messages.
        t = threading.Thread(target=self._AfterOpenReadyWaiter, args=(ws,))
        t.start()


    def _onWsMsg(self, ws, msg):
        try:
            # Parse the incoming message.
            msgObj = json.loads(msg)

            # Check if this is a response to a request
            # info: https://moonraker.readthedocs.io/en/latest/web_api/#json-rpc-api-overview
            if "id" in msgObj:
                with self.JsonRpcIdLock:
                    idInt = int(msgObj["id"])
                    if idInt in self.JsonRpcWaitingContexts:
                        self.Logger.debug("Moonraker RPC response received for request: "+str(idInt))
                        self.JsonRpcWaitingContexts[idInt].SetResultAndEvent(msgObj)
                    else:
                        self.Logger.warn("Moonraker RPC response received for request "+str(idInt) + ", but there is no waiting context.")
                    # If once the response is handled, we are done.
                    return


            # Check for a special message that indicates the klippy connection has been lost.
            # According to the docs, in this case, we should restart the klippy ready process, so we will
            # nuke the WS and start again.
            if "method" in msgObj and msgObj["method"] == "notify_klippy_disconnected":
                self.Logger.info("Moonraker client received notify_klippy_disconnected notification, so we will restart our client connection.")
                self._RestartWebsocket()
                return

            # We use a queue to handle all non reply messages to prevent this thread from getting blocked.
            # The problem is if any of the code paths upstream from the non reply notification tried to issue a request/response
            # they would never get it, because this receive thread would be blocked.
            #
            # If this queue is full, it will throw, but it has a huge capacity, so that would be bad.
            self.NonResponseMsgQueue.put_nowait(msgObj)

        except Exception as e:
            Sentry.Exception("Exception while handing moonraker client websocket message.", e)
            # Raise again which will cause the websocket to close and reset.
            raise e


    def _NonResponseMsgQueueWorker(self):
        try:
            while True:
                # Wait for a message to process.
                msg = self.NonResponseMsgQueue.get()
                # Process and then wait again.
                self._OnWsNonResponseMessage(msg)
        except Exception as e:
            Sentry.Exception("_NonReplyMsgQueueWorker got an exception while handing messages. Killing the websocket. ", e)
        self._RestartWebsocket()


    # Called when the websocket is closed for any reason, connection loss or exception
    def _onWsClose(self, ws):
        self.Logger.info("Connection to moonraker lost.")


    # Called if the websocket hits an error and is closing.
    def _onWsError(self, ws, exception):
        Sentry.Exception("Exception rased from moonraker client websocket connection. The connection will be closed.", exception)


# A helper class used for waiting rpc requests
class JsonRpcWaitingContext:

    def __init__(self, msgId) -> None:
        self.Id = msgId
        self.WaitEvent = threading.Event()
        self.Result = None


    def GetEvent(self):
        return self.WaitEvent


    def GetResult(self):
        return self.Result


    def SetResultAndEvent(self, result):
        self.Result = result
        self.WaitEvent.set()


# The goal of this class it add any needed compatibility logic to allow the moonraker system plugin into the
# common OctoEverywhere logic.
class MoonrakerCompat:

    def __init__(self, logger, printerId) -> None:
        self.Logger = logger

        # This class owns the notification handler.
        # We pass our self as the Printer State Interface
        self.NotificationHandler = NotificationsHandler(self.Logger, self)
        self.NotificationHandler.SetPrinterId(printerId)


    def SetOctoKey(self, octoKey):
        self.NotificationHandler.SetOctoKey(octoKey)


    #
    # Events
    #


    # Called when a new websocket is established to moonraker.
    def OnMoonrakerClientConnected(self):
        # Before we restore state, setup the webcams paths if needed. We do this before since the state setup
        # might fire notifications.
        self._GetSnapshotConfig()

        # This is the hardest of all the calls. The reason being, this call can happen if our service or moonraker restarted, an print
        # can be running while either of those restart. So we need to sync the state here, and make sure things like Gadget and the
        # notification system having their progress threads running correctly.
        self._InitPrintStateForFreshConnect()


    # Called when a new print is starting.
    def OnPrintStart(self, fileName):
        self.NotificationHandler.OnStarted(fileName)


    # Called the the print is paused.
    def OnPrintPaused(self):
        # Get the print filename. If we fail, the pause command accepts None, which will be ignored.
        stats = self._GetCurrentPrintStats()
        fileName = None
        if stats is not None:
            fileName = stats["filename"]
        self.NotificationHandler.OnPaused(fileName)


    # Called the the print is resumed.
    def OnPrintResumed(self):
        # Get the print filename. If we fail, the pause command accepts None, which will be ignored.
        stats = self._GetCurrentPrintStats()
        fileName = None
        if stats is not None:
            fileName = stats["filename"]
        self.NotificationHandler.OnResume(fileName)


    # Called when there's a print percentage progress update.
    def OnPrintProgress(self, progressFloat):
        # Moonraker uses from 0->1 to progress while we assume 100->0
        self.NotificationHandler.OnPrintProgress(None, progressFloat*100.0)


    #
    # Printer State Interface
    #

    # ! Interface Function ! The entire interface must change if the function is changed.
    # This function will get the estimated time remaining for the current print.
    # Returns -1 if the estimate is unknown.
    def GetPrintTimeRemainingEstimateInSeconds(self):
        result = MoonrakerClient.Get().SendJsonRpcRequest("printer.objects.query",
        {
            "objects": {
                "virtual_sdcard": None,
                "print_stats": None
            }
        })
        if result.HasError():
            self.Logger.error("GetPrintTimeRemainingEstimateInSeconds failed to query print objects: "+result.GetLoggingErrorStr())
            return -1
        # This is how the moonraker doc suggests we compute ETA, without a file metadata time.
        # TODO - We should try to do this with the file meta, otherwise on start the ETA is unknown.
        # The downside of the file metadata is I don't know how accurate it is, since it might not know live print settings.
        try:
            res = result.GetResult()["status"]
            printDurationSec = res["print_stats"]["print_duration"]
            progressFloat = res["virtual_sdcard"]["progress"]
            totalTimeSec = printDurationSec / progressFloat
            etaSec = totalTimeSec - printDurationSec
            # On start, printDurationSec will be 0, so this will compute to 0, which makes no sense.
            # So we just return unknown.
            if totalTimeSec < 0.0001:
                return -1
            return int(etaSec)
        except Exception as e:
            Sentry.Exception("GetPrintTimeRemainingEstimateInSeconds exception while computing ETA. ", e)
        return -1


    # ! Interface Function ! The entire interface must change if the function is changed.
    # Returns the current zoffset if known, otherwise -1.
    def GetCurrentZOffset(self):
        # TODO
        return -1


    # ! Interface Function ! The entire interface must change if the function is changed.
    # Returns True if the printing timers (notifications and gadget) should be running, which is only the printing state. (not even paused)
    # False if the printer state is anything else, which means they should stop.
    def ShouldPrintingTimersBeRunning(self):
        stats = self._GetCurrentPrintStats()
        if stats is None:
            self.Logger.warn("ShouldPrintingTimersBeRunning failed to get current state.")
            return True
        # For moonraker, printing is the only state we want to allow the timers to run in.
        # All other states will resume them when moved out of.
        return stats["state"] == "printing"


    # ! Interface Function ! The entire interface must change if the function is changed.
    # If called while the print state is "Printing", returns True if the print is currently in the warm-up phase. Otherwise False
    def IsPrintWarmingUp(self):
        # For moonraker, we have found that if the print_stats reports a state of "printing"
        # but the "print_duration" is still 0, it means we are warming up. print_duration is the time actually spent printing
        # so it doesn't increment while the system is heating.
        result = MoonrakerClient.Get().SendJsonRpcRequest("printer.objects.query",
        {
            "objects": {
                "print_stats": None
            }
        })
        if result.HasError():
            self.Logger.error("IsPrintWarmingUp failed to query print objects: "+result.GetLoggingErrorStr())
            return False
        try:
            res = result.GetResult()["status"]
            state = res["print_stats"]["state"]
            printDurationSec = res["print_stats"]["print_duration"]
            if state == "printing" and printDurationSec < 0.00001:
                return True
            return False
        except Exception as e:
            Sentry.Exception("IsPrintWarmingUp exception. ", e)
        return False


    #
    # Helpers
    #

    def _InitPrintStateForFreshConnect(self):
        # Get the current state
        stats = self._GetCurrentPrintStats()
        if stats is None:
            self.Logger.error("Moonraker client init sync failed to get the printer state.")
            return

        # What this logic is trying to do is re-sync the notification handler with the current state.
        # The only tricky part is if there's an on-going print that we aren't tracking, we need to restore
        # the state as well as possible to get notifications in sync.
        state = stats["state"]
        fileName_CanBeNone = stats["filename"]
        totalDurationFloatSec_CanBeNone = stats["total_duration"] # Use the total duration
        self.Logger.info("Printer state at socket connect is: "+state)
        self.NotificationHandler.OnRestorePrintIfNeeded(state, fileName_CanBeNone, totalDurationFloatSec_CanBeNone)


    # Queries moonraker for the current printer stats.
    # Returns null if the call falls or the resulting object DOESN'T contain at least: filename, state, total_duration, print_duration
    def _GetCurrentPrintStats(self):
        result = MoonrakerClient.Get().SendJsonRpcRequest("printer.objects.query",
        {
            "objects": {
                "print_stats": None
            }
        })
        # Validate
        if result.HasError():
            self.Logger.error("Moonraker client failed _GetCurrentPrintStats. "+result.GetLoggingErrorStr())
            return None
        res = result.GetResult()
        if "status" not in res or "print_stats" not in res["status"]:
            self.Logger.error("Moonraker client didn't find status in _GetCurrentPrintStats.")
            return None
        printStats = res["status"]["print_stats"]
        if "state" not in printStats or "filename" not in printStats or "total_duration" not in printStats or "print_duration" not in printStats:
            self.Logger.error("Moonraker client didn't find required field in _GetCurrentPrintStats. "+json.dumps(printStats))
            return None
        return printStats


    # Queries moonraker for the camera config, and if found, setups up the snapshot system from it's defaults.
    def _GetSnapshotConfig(self):
        # Check the first source.
        # TODO
        # result = MoonrakerClient.Get().SendJsonRpcRequest("server.webcams.list", {
        #     "namespace": "webcams",
        # })
        pass