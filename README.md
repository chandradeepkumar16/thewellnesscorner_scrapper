# WellnessCorner Cart API Scraper

This project runs a Python script that connects to a Chrome instance via
Chrome DevTools Protocol (CDP) to capture network responses from the
WellnessCorner website and extract SKU IDs from cart-related APIs.

------------------------------------------------------------------------

# Prerequisites

Before running the script, ensure the following are installed:

-   Python 3.8 or higher
-   Google Chrome
-   Required Python dependencies

Install dependencies:

pip install -r requirements.txt

------------------------------------------------------------------------

# How the Script Works

1.  Chrome is started with Remote Debugging enabled.
2.  The Python script attaches to Chrome using CDP.
3.  Network requests are captured from the website.
4.  Cart-related API responses are filtered.
5.  Extracted SKU IDs are saved to a file.

------------------------------------------------------------------------

# Steps to Run the Script

## 1. Kill Existing Chrome Processes

taskkill /IM chrome.exe /F

------------------------------------------------------------------------

## 2. Start Chrome with Remote Debugging Enabled

&
"C:`\Program `{=tex}Files`\Google`{=tex}`\Chrome`{=tex}`\Application`{=tex}`\chrome`{=tex}.exe"
--remote-debugging-port=9222 --user-data-dir="\$env:TEMP`\wc`{=tex}-cdp"

It will look like this :

taskkill /IM chrome.exe /F
& "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="$env:TEMP\wc-cdp"
cd "e:\Codes-Projects\wellnesscorner scrapper"
python main.py --live-url https://www.thewellnesscorner.com/ --cart-only --attach-cdp-url http://127.0.0.1:9222 --save-captured-json captured.json --output skuids.txt


Explanation:

-   --remote-debugging-port=9222 : Opens Chrome DevTools debugging port
-   --user-data-dir : Uses a temporary Chrome profile

------------------------------------------------------------------------

## 3. Navigate to Project Directory

cd "e:`\Codes`{=tex}-Projects`\wellnesscorner `{=tex}scrapper"

------------------------------------------------------------------------

## 4. Run the Python Script

python main.py --live-url https://www.thewellnesscorner.com/ --cart-only
--attach-cdp-url http://127.0.0.1:9222 --save-captured-json
captured.json --output skuids.txt

------------------------------------------------------------------------

# Command Parameters

  Parameter              Description
  ---------------------- ---------------------------------------
  --live-url             Target website URL
  --cart-only            Filters only cart related API calls
  --attach-cdp-url       CDP endpoint used to attach to Chrome
  --save-captured-json   Saves captured network responses
  --output               File to store extracted SKU IDs

------------------------------------------------------------------------

# Output Files

After execution, the following files will be generated:

  File            Description
  --------------- ----------------------------
  captured.json   Raw captured API responses
  skuids.txt      Extracted SKU IDs

------------------------------------------------------------------------

# Complete Command Sequence

taskkill /IM chrome.exe /F

&
"C:`\Program `{=tex}Files`\Google`{=tex}`\Chrome`{=tex}`\Application`{=tex}`\chrome`{=tex}.exe"
--remote-debugging-port=9222 --user-data-dir="\$env:TEMP`\wc`{=tex}-cdp"

cd "e:`\Codes`{=tex}-Projects`\wellnesscorner `{=tex}scrapper"

python main.py --live-url https://www.thewellnesscorner.com/ --cart-only
--attach-cdp-url http://127.0.0.1:9222 --save-captured-json
captured.json --output skuids.txt

------------------------------------------------------------------------

# Troubleshooting

### Chrome not connecting to CDP

Ensure Chrome was started with:

--remote-debugging-port=9222

### Port already in use

taskkill /IM chrome.exe /F

Then restart Chrome.

### Script not capturing APIs

Ensure: - Chrome is open - Debugging port is running - Correct CDP URL
is used: http://127.0.0.1:9222
