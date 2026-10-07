# Chrome Extension - Final Setup Guide

## ✅ Issue Fixed

**Root Cause**: The extension was in a confusing nested location:
```
Auto_job_apply/Auto_job_apply/extension  ← TOO DEEP (3 levels)
```

**Solution**: Moved to a simpler, direct location:
```
Auto_job_apply/extension  ← SIMPLE (2 levels)
```

---

## 📍 Extension Location (FINAL)

```
C:\Users\Madan A\OneDrive\Documents\Desktop\Auto_job_apply\extension
```

This is now the ONLY path you need to remember!

---

## 🚀 Load Extension in Chrome

### Method 1: Automatic (Easiest)
1. Double-click: `LOAD_EXTENSION.bat` (in Desktop/Auto_job_apply folder)
2. Chrome opens automatically
3. Follow the instructions displayed

### Method 2: Manual
1. Open Chrome
2. Go to: `chrome://extensions/`
3. Toggle "Developer mode" ON (top-right corner, turns blue)
4. Click "Load unpacked"
5. Navigate to Desktop → Auto_job_apply → select `extension` folder
6. Click "Select Folder"

### Method 3: Copy-Paste
In Chrome's folder picker, paste this in the address bar:
```
C:\Users\Madan A\OneDrive\Documents\Desktop\Auto_job_apply\extension
```

Then press Enter and click "Select Folder"

---

## ✅ Verify Extension Loaded Successfully

You should see:
- Extension appears in your Chrome extensions list
- Extension icon appears in your toolbar
- No error messages
- Page shows "AI Job Application Autofiller v1.0.0"

---

## ⚙️ Configure the Extension

1. Click the extension icon in your toolbar
2. **Add Gemini API Key:**
   - Get free key from: https://aistudio.google.com/app/apikey
   - Paste into "API Key" field
3. **Add Your Resume:**
   - Paste your professional summary or resume text
   - Minimum 50 characters
4. **Click "Save Settings"**

---

## 🎯 Start Using

Visit any job application site:
- LinkedIn Jobs
- Workday
- Naukri.com
- Greenhouse
- Lever
- Other ATS platforms

Look for:
- **"✨" buttons** next to text fields → Click to fill that field
- **"⚡ Fill All" button** (bottom-right) → Click to fill entire form

Status messages appear in bottom-right corner showing progress.

---

## 📊 Verify Setup with Diagnostic

Run this command to verify everything:
```bash
python main.py --mode test
```

You should see:
```
[+] Chrome Extension  [+] Ready. Load: C:\Users\Madan A\OneDrive\...\extension
```

---

## ❌ If Still Getting Error

This error should NOT happen again because:
1. ✅ Extension moved to simpler location
2. ✅ Diagnostic updated to find new location
3. ✅ Path is now only 2 levels deep (not 3)
4. ✅ No more "Auto_job_apply\Auto_job_apply" nesting

**If you still get an error:**

1. Make sure you're selecting the folder that contains `manifest.json` directly
2. Don't double-click the folder - just select it and click "Select Folder"
3. Check that "Developer mode" is ON (blue toggle)
4. Try refreshing `chrome://extensions/` page

---

## 📁 Extension Contents

The `extension` folder contains:
```
extension/
├── manifest.json        ← Chrome looks for this
├── popup.html          ← Settings UI
├── popup.js            ← Settings logic
├── content.js          ← Form automation
├── gemini.js           ← Gemini API client
├── background.js       ← Service worker
├── .env.example        ← Credential template
├── resume.txt.example  ← Resume template
└── README.md           ← Feature docs
```

---

## 🆘 Support

**For technical issues:**
```bash
python main.py --mode test
```

**Check console errors:**
- Open DevTools: F12
- Go to "Console" tab
- Look for any error messages

**For more help:**
- See `EXTENSION_SIMPLE_INSTRUCTIONS.txt`
- See `extension/INSTALL.md` for detailed guide
- See `extension/README.md` for features

---

## ✨ You're All Set!

The extension is now:
- ✅ In a simple, direct location
- ✅ Easy to find in Chrome
- ✅ Ready to configure
- ✅ Ready to use on job sites

Good luck with your job applications! 🚀
