# Chrome Extension - Setup & Loading Guide

**Status**: ✅ **READY TO LOAD**  
**Date**: 2026-07-20  
**Extension Name**: AI Job Application Autofiller

---

## What Was Fixed

### Issue: "Could not load icon 'icons/icon-16.png' specified in 'icons'"

**Root Cause**: Missing `icons/` directory and PNG files referenced in `manifest.json`

**Solution Applied**:
1. ✅ Created `chrome_extension/icons/` directory
2. ✅ Generated valid PNG files:
   - `icon-16.png` (16x16 pixels) - 170 bytes
   - `icon-48.png` (48x48 pixels) - 354 bytes
   - `icon-128.png` (128x128 pixels) - 880 bytes
3. ✅ Verified all icon paths in manifest.json
4. ✅ Confirmed all referenced files exist

---

## Extension Structure

```
chrome_extension/
├── manifest.json          (v3 configuration)
├── popup.html             (UI panel)
├── popup.js               (Panel logic)
├── background.js          (Service worker)
├── content.js             (Page content injection)
├── gemini.js              (AI integration)
└── icons/                 (NEW - Icon files)
    ├── icon-16.png        (16x16 toolbar icon)
    ├── icon-48.png        (48x48 context menu icon)
    └── icon-128.png       (128x128 installation icon)
```

---

## File Verification Report

### manifest.json Structure
- ✅ **Name**: AI Job Application Autofiller
- ✅ **Version**: 1.0.0
- ✅ **Manifest Version**: 3 (Chrome latest)

### Referenced Files
- ✅ `popup.html` - Extension UI
- ✅ `popup.js` - UI logic
- ✅ `background.js` - Service worker
- ✅ `content.js` - Content script
- ✅ `gemini.js` - AI engine

### Icons
- ✅ `icons/icon-16.png` - Valid 16x16 PNG
- ✅ `icons/icon-48.png` - Valid 48x48 PNG
- ✅ `icons/icon-128.png` - Valid 128x128 PNG

### Permissions
- ✅ `activeTab` - Access current tab
- ✅ `scripting` - Inject scripts
- ✅ `storage` - Local storage
- ✅ `https://generativelanguage.googleapis.com/*` - Gemini API

### Content Scripts
- ✅ Workday (https://www.workday.com/*)
- ✅ LinkedIn (https://www.linkedin.com/*)
- ✅ Naukri (https://www.naukri.com/*)
- ✅ Greenhouse (https://*.greenhouse.io/*)
- ✅ Lever (https://*.lever.co/*)

---

## How to Load Extension in Chrome

### Step 1: Open Extension Management Page
1. Open Chrome
2. Go to: `chrome://extensions/`
3. OR: Menu → More Tools → Extensions

### Step 2: Enable Developer Mode
1. Toggle **"Developer mode"** in the top-right corner
2. The page will refresh with additional options

### Step 3: Load Unpacked Extension
1. Click **"Load unpacked"** button
2. Navigate to: `C:\Users\Madan A\OneDrive\Documents\Desktop\Auto_job_apply\chrome_extension\`
3. Click **"Select Folder"**

### Step 4: Verify Extension Loaded
- ✅ Extension should appear in the list
- ✅ Icon appears in Chrome toolbar (top-right)
- ✅ No error messages
- ✅ ID assigned (e.g., `hkplhkokomlhjfcjfjdadkbpebjllkhe`)

---

## Extension Details

### Default Popup Action
- Clicking the extension icon opens `popup.html`
- Title: "AI Job Autofiller"
- Icon: Auto-detected from manifest

### Content Scripts
Automatically injected into matching job application sites:
- Workday platforms
- LinkedIn job pages
- Naukri job pages
- Greenhouse ATS platforms
- Lever career pages

### Background Service Worker
- Runs continuously in background
- Handles messages from content scripts
- Manages Gemini API calls
- Stores data in Chrome storage API

---

## Troubleshooting

### Error: "Could not load icon"
**Solution**: Already fixed! Icons are now present in `icons/` directory.

### Error: "Manifest parsing error"
**Solution**: Manifest.json structure is valid. Check for:
- JSON syntax errors (use JSON validator)
- File encoding (should be UTF-8)
- Correct quotes (use double quotes)

### Extension doesn't appear after loading
**Solution**:
1. Refresh `chrome://extensions/` page (Ctrl+Shift+Delete)
2. Clear Chrome cache
3. Restart Chrome
4. Try reloading unpacked folder

### Popup doesn't open
**Solution**:
1. Right-click extension icon → Inspect popup
2. Check console for JavaScript errors
3. Verify `popup.html` and `popup.js` exist
4. Check browser console (F12) for service worker errors

### API calls fail
**Solution**:
1. Verify GEMINI_API_KEY in `.env` (main project)
2. Check permissions in manifest.json
3. Look for CORS errors in console
4. Verify Gemini API service is accessible

### Extension permissions denied
**Solution**:
1. Ensure all required permissions are in manifest.json
2. Reload unpacked extension
3. Chrome will re-request permissions
4. Click "Allow" when prompted

---

## Testing the Extension

### Quick Test Steps

1. **Verify Icon Visible**
   - Look for "AI Job Autofiller" icon in Chrome toolbar
   - It should have blue and white colors

2. **Open Popup**
   - Click the extension icon
   - Popup window should appear without errors

3. **Test on Job Site**
   - Go to: https://www.linkedin.com/jobs/
   - Open DevTools (F12)
   - Check Console tab
   - Content script should have injected successfully

4. **Monitor Background Worker**
   - In `chrome://extensions/`, find your extension
   - Click "Service Worker" link in Details section
   - Verify no errors in console

---

## Features Once Loaded

### Automatic Detection
- Extension detects job application forms
- Identifies input fields
- Recognizes screening questions

### AI-Powered Filling
- Sends questions to Gemini API
- Generates contextual answers from resume
- Auto-fills text fields

### Manual Controls
- Popup UI for manual triggering
- Override AI answers if needed
- Save preferences

### Data Storage
- Stores settings in Chrome storage
- Syncs across Chrome instances
- No cloud upload of personal data

---

## Security Notes

⚠️ **Important Security Considerations**:

- **API Keys**: Stored in `.env` (not in extension code)
- **Personal Data**: Stored locally only
- **Network**: Only communicates with Gemini API (via manifest permissions)
- **Permissions**: Limited to specific job application sites
- **Injection**: Content scripts run in isolated context

---

## Files Changed

### Created
- `chrome_extension/icons/` directory
- `chrome_extension/icons/icon-16.png`
- `chrome_extension/icons/icon-48.png`
- `chrome_extension/icons/icon-128.png`

### Reviewed (No Changes Needed)
- `manifest.json` ✅ (Paths are correct)
- `popup.html` ✅ (Valid)
- `popup.js` ✅ (Valid)
- `background.js` ✅ (Valid)
- `content.js` ✅ (Valid)
- `gemini.js` ✅ (Valid)

---

## Next Steps

1. **Load Extension**
   - Follow "How to Load Extension in Chrome" section above
   - Should load without any errors

2. **Add API Key** (if not already set)
   - Edit `.env` in main project folder
   - Add your Gemini API key

3. **Test on Job Sites**
   - Navigate to LinkedIn, Naukri, etc.
   - Test form auto-filling

4. **Monitor Extension**
   - Keep `chrome://extensions/` open during testing
   - Check for any error messages

---

## Resources

- **Manifest v3 Docs**: https://developer.chrome.com/docs/extensions/mv3/
- **Extension API**: https://developer.chrome.com/docs/extensions/reference/
- **Content Scripts**: https://developer.chrome.com/docs/extensions/mv3/content_scripts/
- **Service Workers**: https://developer.chrome.com/docs/extensions/mv3/service_workers/

---

## Support

For issues with the extension:

1. **Check manifest.json syntax** - Use JSON validator
2. **Review Chrome console** - F12 → Console tab
3. **Check service worker logs** - `chrome://extensions/` → Service Worker link
4. **Verify file paths** - All files must exist in correct locations
5. **Review error messages** - Chrome provides specific error details

---

**Status**: ✅ Extension is ready to load!

All icon files are valid, manifest is correct, and all paths are properly configured.
