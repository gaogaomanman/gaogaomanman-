' _launch.vbs - start a node server in hidden window, redirecting output to log.
' Usage: wscript _launch.vbs "workdir" "script.js" "logfile" [node.exe path]
' Note: run node directly (NOT via cmd /c) because cmd /c + quoted args fails in WSH Run.
Option Explicit
Dim ws, wd, script, log, nodeExe, cmd
If WScript.Arguments.Count < 3 Then
    WScript.Echo "Usage: wscript _launch.vbs ""workdir"" ""script"" ""logfile"" [node.exe]"
    WScript.Quit 1
End If
wd = WScript.Arguments(0)
script = WScript.Arguments(1)
log = WScript.Arguments(2)
nodeExe = "node"
If WScript.Arguments.Count >= 4 And WScript.Arguments(3) <> "" Then
    nodeExe = WScript.Arguments(3)
End If
' Directly run node with output redirection (window style 0 = hidden, False = do not wait)
cmd = """" & nodeExe & """ """ & script & """ > """ & log & """ 2>&1"
Set ws = CreateObject("WScript.Shell")
ws.CurrentDirectory = wd
ws.Run cmd, 0, False
