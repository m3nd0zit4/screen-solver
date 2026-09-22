' Starts Screen Solver hidden (no console window).
Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
CreateObject("WScript.Shell").Run """" & dir & "\.venv\Scripts\pythonw.exe"" """ & dir & "\app.py""", 0, False
