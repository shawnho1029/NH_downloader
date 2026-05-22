@echo off
chcp 65001 >nul

cd /d F:\code\Comic_downloader\NH_downloader

set "page_range=1,10"
set /p "user_range=Favorites page range, e.g. 1,10 (press Enter for 1,10): "
if not "%user_range%"=="" set "page_range=%user_range%"

set "zip_arg="
set /p "keep_zip=Keep ZIP files after extraction? (y/N): "
if /I "%keep_zip%"=="y" set "zip_arg=--keep-zip"
if /I "%keep_zip%"=="yes" set "zip_arg=--keep-zip"

python nh_downloader.py favorites sync-run --page-range "%page_range%" %zip_arg%

echo.
echo Done.
pause
