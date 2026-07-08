' Boot-tier autostart: run the Mission Control server invisibly at logon.
' Safe to run twice - server.py exits immediately if port 8799 is already bound.
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
sh.Run "python server.py", 0, False
