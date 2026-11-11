Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "d:\ПРоекты ИИ\SchoolsByBot"
WshShell.Run """C:\Users\sveta\AppData\Local\Programs\Python\Python312\pythonw.exe"" ""d:\ПРоекты ИИ\SchoolsByBot\bot.py""", 0, False
