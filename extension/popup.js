class PopupManager {
    constructor() {
        this.apiKeyInput = document.getElementById('api-key');
        this.resumeInput = document.getElementById('resume-text');
        this.jobTitlesInput = document.getElementById('job-titles');

        this.apiStatusBadge = document.getElementById('api-status');
        this.resumeStatusBadge = document.getElementById('resume-status');
        this.titlesStatusBadge = document.getElementById('titles-status');

        this.saveBtn = document.getElementById('save-btn');
        this.resetBtn = document.getElementById('reset-btn');

        this.toggleApiKeyBtn = document.getElementById('toggle-api-key');

        this.initialize();
    }

    async initialize() {
        await this.loadSettings();
        this.attachEventListeners();
    }

    async loadSettings() {
        const data = await chrome.storage.local.get([
            'apiKey',
            'resumeText',
            'jobTitles',
        ]);

        if (data.apiKey) {
            this.apiKeyInput.value = data.apiKey;
            this.updateStatusBadge(this.apiStatusBadge, true);
        }

        if (data.resumeText) {
            this.resumeInput.value = data.resumeText;
            this.updateStatusBadge(this.resumeStatusBadge, true);
        }

        if (data.jobTitles) {
            this.jobTitlesInput.value = data.jobTitles;
            this.updateStatusBadge(this.titlesStatusBadge, true);
        }
    }

    attachEventListeners() {
        this.saveBtn.addEventListener('click', () => this.saveSettings());
        this.resetBtn.addEventListener('click', () => this.resetSettings());
        this.toggleApiKeyBtn.addEventListener('click', () =>
            this.togglePasswordVisibility()
        );

        this.apiKeyInput.addEventListener('input', () =>
            this.updateStatusBadge(this.apiStatusBadge, false)
        );
        this.resumeInput.addEventListener('input', () =>
            this.updateStatusBadge(this.resumeStatusBadge, false)
        );
        this.jobTitlesInput.addEventListener('input', () =>
            this.updateStatusBadge(this.titlesStatusBadge, false)
        );
    }

    updateStatusBadge(badge, saved) {
        if (saved) {
            badge.textContent = '✓ Saved';
            badge.classList.remove('unsaved');
            badge.classList.add('saved');
        } else {
            badge.textContent = 'Unsaved';
            badge.classList.remove('saved');
            badge.classList.add('unsaved');
        }
    }

    async saveSettings() {
        const apiKey = this.apiKeyInput.value.trim();
        const resumeText = this.resumeInput.value.trim();
        const jobTitles = this.jobTitlesInput.value.trim();

        if (!apiKey) {
            this.showAlert('⚠️ API Key is required', 'error');
            return;
        }

        if (apiKey.length < 20) {
            this.showAlert('⚠️ API Key looks incomplete (too short)', 'warning');
            return;
        }

        if (!resumeText) {
            this.showAlert('⚠️ Resume text is required', 'error');
            return;
        }

        if (resumeText.length < 50) {
            this.showAlert('⚠️ Resume is too short (minimum 50 chars)', 'warning');
            return;
        }

        try {
            this.saveBtn.disabled = true;
            this.saveBtn.textContent = '💾 Saving...';

            await chrome.storage.local.set({
                apiKey,
                resumeText,
                jobTitles: jobTitles || 'Software Engineer, Backend Developer, SDE',
            });

            this.updateStatusBadge(this.apiStatusBadge, true);
            this.updateStatusBadge(this.resumeStatusBadge, true);
            this.updateStatusBadge(this.titlesStatusBadge, true);

            this.showAlert('✓ Settings saved successfully!', 'success');

            setTimeout(() => {
                this.saveBtn.disabled = false;
                this.saveBtn.textContent = '💾 Save Settings';
            }, 2000);
        } catch (error) {
            console.error('Save error:', error);
            this.showAlert(
                `❌ Save failed: ${error.message}`,
                'error'
            );

            this.saveBtn.disabled = false;
            this.saveBtn.textContent = '💾 Save Settings';
        }
    }

    resetSettings() {
        if (confirm('❌ Reset all settings? This cannot be undone.')) {
            this.apiKeyInput.value = '';
            this.resumeInput.value = '';
            this.jobTitlesInput.value = '';

            chrome.storage.local.remove(['apiKey', 'resumeText', 'jobTitles']);

            this.updateStatusBadge(this.apiStatusBadge, false);
            this.updateStatusBadge(this.resumeStatusBadge, false);
            this.updateStatusBadge(this.titlesStatusBadge, false);

            this.showAlert('↺ Settings reset to defaults', 'info');
        }
    }

    togglePasswordVisibility() {
        const isPassword = this.apiKeyInput.type === 'password';
        this.apiKeyInput.type = isPassword ? 'text' : 'password';
        this.toggleApiKeyBtn.textContent = isPassword ? 'Hide' : 'Show';
    }

    showAlert(message, type = 'info') {
        const alert = document.createElement('div');

        const styles = {
            info: 'background-color: #3b82f6; color: white;',
            success: 'background-color: #22c55e; color: white;',
            warning: 'background-color: #f59e0b; color: white;',
            error: 'background-color: #ef4444; color: white;',
        };

        alert.style.cssText = `
            position: fixed;
            top: 8px;
            right: 8px;
            padding: 10px 14px;
            ${styles[type] || styles.info}
            border-radius: 6px;
            font-size: 12px;
            font-weight: 500;
            z-index: 9999;
            animation: slideIn 0.3s ease-out;
            max-width: 280px;
            word-wrap: break-word;
            box-shadow: 0 2px 8px rgba(0, 0, 0, 0.2);
        `;

        alert.textContent = message;
        document.body.appendChild(alert);

        setTimeout(() => {
            alert.style.animation = 'slideOut 0.3s ease-out';
            setTimeout(() => alert.remove(), 300);
        }, 4000);
    }
}

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => {
        new PopupManager();
    });
} else {
    new PopupManager();
}
