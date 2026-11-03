WinSW v2.12.0 (WinSW.NET461.exe) - Windows Service Wrapper
Source:  https://github.com/winsw/winsw/releases/tag/v2.12.0
License: MIT (see LICENSE.txt)
Needs:   .NET Framework 4.6.1 or later (built into Windows 10 1607+, Windows 11, Windows 11 IoT Enterprise LTSC)

DTM uses it through:  python -m dtm -c config.toml service install
(run cmd "as administrator"). It copies this exe to service\DTM.exe and service\DTM-Watchdog.exe
and writes the matching XML files there.
SHA256:  b5066b7bbdfba1293e5d15cda3caaea88fbeab35bd5b38c41c913d492aadfc4f
