Set objShell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
strDir = fso.GetParentFolderName(WScript.ScriptFullName)
objShell.CurrentDirectory = strDir

pythonwPath = "C:\Users\thieu\AppData\Local\Programs\Python\Python312\pythonw.exe"
If Not fso.FileExists(pythonwPath) Then
    pythonwPath = objShell.ExpandEnvironmentStrings("%LocalAppData%\Programs\Python\Python312\pythonw.exe")
End If
If Not fso.FileExists(pythonwPath) Then
    pythonwPath = "pythonw.exe"
End If

objShell.Run Chr(34) & pythonwPath & Chr(34) & " " & Chr(34) & strDir & "\app.py" & Chr(34), 1, False
