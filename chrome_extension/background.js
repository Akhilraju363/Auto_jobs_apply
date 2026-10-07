chrome.runtime.onInstalled.addListener(() => {
    chrome.storage.local.get(['apiKey'], (result) => {
        if (!result.apiKey) {
            chrome.action.setBadgeText({ text: '!' });
            chrome.action.setBadgeBackgroundColor({ color: '#ff9800' });
        }
    });
});

chrome.storage.local.onChanged.addListener((changes) => {
    if (changes.apiKey) {
        if (changes.apiKey.newValue) {
            chrome.action.setBadgeText({ text: '' });
        } else {
            chrome.action.setBadgeText({ text: '!' });
            chrome.action.setBadgeBackgroundColor({ color: '#ff9800' });
        }
    }
});

chrome.runtime.onMessage.addListener((request, sender, sendResponse) => {
    if (request.action === 'callGeminiAPI') {
        handleGeminiAPICall(request.payload, request.apiKey)
            .then((result) => {
                sendResponse({ success: true, data: result });
            })
            .catch((error) => {
                sendResponse({ success: false, error: error.message });
            });

        return true;
    }
});

async function handleGeminiAPICall(payload, apiKey) {
    const baseUrl = 'https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent';

    const response = await fetch(`${baseUrl}?key=${apiKey}`, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
        },
        body: JSON.stringify(payload),
    });

    if (!response.ok) {
        const error = await response.json();
        const errorMessage = error.error?.message || `HTTP ${response.status}`;
        throw new Error(errorMessage);
    }

    const data = await response.json();
    return data;
}
