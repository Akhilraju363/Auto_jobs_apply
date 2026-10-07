class UniversalATSFormEngine {
    constructor() {
        this.apiKey = null;
        this.resumeText = null;
        this.userProfile = {};
        this.gemini = null;
        this.isProcessing = false;
        this.processedForms = new WeakSet();
        this.toastContainer = null;

        this.initialize();
    }

    async initialize() {
        this.initializeToastContainer();
        await this.loadSettings();
        this.validateConfiguration();
        this.scanAndProcessForms();
        this.setupSPANavigation();
    }

    initializeToastContainer() {
        this.toastContainer = document.createElement('div');
        this.toastContainer.id = 'ai-toast-container';
        this.toastContainer.style.cssText = `
            position: fixed;
            bottom: 20px;
            right: 20px;
            z-index: 999999;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        `;
        document.body.appendChild(this.toastContainer);
    }

    async loadSettings() {
        const data = await chrome.storage.local.get([
            'apiKey',
            'resumeText',
            'userProfile',
        ]);

        this.apiKey = data.apiKey;
        this.resumeText = data.resumeText;
        this.userProfile = data.userProfile || {};

        if (this.apiKey) {
            this.gemini = new GeminiClient(this.apiKey);
        }
    }

    validateConfiguration() {
        if (!this.apiKey) {
            this.showStatusToast(
                '⚠️ Gemini API key not found. Click the JobFlow extension icon to configure.',
                'error',
                6000
            );
            return false;
        }

        if (!this.resumeText) {
            this.showStatusToast(
                '⚠️ Resume not configured. Update in the extension popup.',
                'warning',
                6000
            );
            return false;
        }

        return true;
    }

    showStatusToast(message, type = 'info', duration = 4000) {
        try {
            const toast = document.createElement('div');
            const icons = {
                info: 'ℹ️',
                success: '✓',
                error: '❌',
                warning: '⚠️',
                loading: '⏳',
            };

            toast.className = `ai-toast ai-toast-${type}`;
            toast.innerHTML = `${icons[type] || type} ${message}`;

            const baseStyles = `
                display: inline-block;
                padding: 12px 16px;
                margin-bottom: 8px;
                border-radius: 8px;
                font-size: 13px;
                font-weight: 500;
                backdrop-filter: blur(10px);
                border: 1px solid rgba(255, 255, 255, 0.2);
                color: white;
                box-shadow: 0 4px 12px rgba(0, 0, 0, 0.3);
                animation: slideIn 0.3s ease-out;
                word-wrap: break-word;
                max-width: 300px;
            `;

            const typeStyles = {
                info: 'background: rgba(59, 130, 246, 0.8); border-color: rgba(59, 130, 246, 0.3);',
                success: 'background: rgba(34, 197, 94, 0.8); border-color: rgba(34, 197, 94, 0.3);',
                error: 'background: rgba(239, 68, 68, 0.8); border-color: rgba(239, 68, 68, 0.3);',
                warning: 'background: rgba(245, 158, 11, 0.8); border-color: rgba(245, 158, 11, 0.3);',
                loading: 'background: rgba(168, 85, 247, 0.8); border-color: rgba(168, 85, 247, 0.3);',
            };

            toast.style.cssText = baseStyles + (typeStyles[type] || typeStyles.info);

            if (!this.toastContainer) {
                this.initializeToastContainer();
            }

            this.toastContainer.appendChild(toast);

            if (duration > 0) {
                setTimeout(() => {
                    toast.style.animation = 'slideOut 0.3s ease-out';
                    setTimeout(() => toast.remove(), 300);
                }, duration);
            }

            this.ensureAnimationStyles();
        } catch (e) {
            console.error('Toast error:', e);
        }
    }

    ensureAnimationStyles() {
        if (!document.getElementById('ai-toast-styles')) {
            const style = document.createElement('style');
            style.id = 'ai-toast-styles';
            style.textContent = `
                @keyframes slideIn {
                    from {
                        transform: translateX(400px);
                        opacity: 0;
                    }
                    to {
                        transform: translateX(0);
                        opacity: 1;
                    }
                }
                @keyframes slideOut {
                    from {
                        transform: translateX(0);
                        opacity: 1;
                    }
                    to {
                        transform: translateX(400px);
                        opacity: 0;
                    }
                }
            `;
            document.head.appendChild(style);
        }
    }

    setupSPANavigation() {
        let lastUrl = location.href;

        const urlObserver = setInterval(() => {
            const currentUrl = location.href;
            if (currentUrl !== lastUrl) {
                lastUrl = currentUrl;
                setTimeout(() => this.scanAndProcessForms(), 500);
            }
        }, 1000);

        const mutationObserver = new MutationObserver(() => {
            if (Math.random() < 0.05) {
                this.scanAndProcessForms();
            }
        });

        try {
            mutationObserver.observe(document.body, {
                childList: true,
                subtree: true,
            });
        } catch (e) {
            console.warn('Mutation observer setup failed:', e);
        }
    }

    scanAndProcessForms() {
        try {
            const formSelectors = [
                'form',
                '[role="form"]',
                '[data-form]',
                '.form-container',
                '[class*="form"]',
            ];

            const forms = document.querySelectorAll(formSelectors.join(','));

            forms.forEach((form) => {
                if (!this.processedForms.has(form)) {
                    this.processForm(form);
                    this.processedForms.add(form);
                }
            });
        } catch (e) {
            console.error('Form scanning error:', e);
        }
    }

    processForm(form) {
        const fields = this.detectFormFields(form);

        if (fields.length === 0) return;

        fields.forEach((field) => {
            this.injectFieldButton(field.element, field.label);
        });

        this.injectFormLevelButton(form, fields);
    }

    detectFormFields(form) {
        const fields = [];

        try {
            const inputs = form.querySelectorAll(
                'input[type="text"], input[type="email"], input[type="tel"], input[type="number"], textarea, [contenteditable="true"]'
            );

            inputs.forEach((input) => {
                if (this.isFieldFilled(input)) return;

                const label = this.getLabelTextForElement(input);
                if (label && this.isJobApplicationField(label)) {
                    fields.push({ element: input, label });
                }
            });
        } catch (e) {
            console.error('Field detection error:', e);
        }

        return fields;
    }

    getLabelTextForElement(element) {
        try {
            if (element.id) {
                const label = document.querySelector(`label[for="${element.id}"]`);
                if (label?.textContent) {
                    return label.textContent.trim();
                }
            }

            const parentLabel = element.closest('label');
            if (parentLabel?.textContent) {
                return parentLabel.textContent.trim();
            }

            if (element.getAttribute('aria-label')) {
                return element.getAttribute('aria-label').trim();
            }

            if (element.placeholder) {
                return element.placeholder.trim();
            }

            if (element.title) {
                return element.title.trim();
            }

            let prev = element.previousElementSibling;
            while (prev && prev !== element) {
                if (prev.tagName === 'LABEL' && prev.textContent) {
                    return prev.textContent.trim();
                }
                if (prev.textContent && prev.textContent.trim()) {
                    return prev.textContent.trim().substring(0, 150);
                }
                prev = prev.previousElementSibling;
            }

            const parent = element.parentElement;
            if (parent) {
                const labels = parent.querySelectorAll('label, .label, [class*="label"]');
                if (labels.length > 0 && labels[0].textContent) {
                    return labels[0].textContent.trim();
                }
            }

            if (element.name) return element.name.trim();
            if (element.id) return element.id.trim();

            return '';
        } catch (e) {
            console.warn('Label extraction error:', e);
            return element.name || element.id || '';
        }
    }

    isFieldFilled(element) {
        try {
            if (element.tagName === 'TEXTAREA') {
                return element.value?.trim().length > 0;
            }
            if (element.contentEditable === 'true') {
                return element.textContent?.trim().length > 0;
            }
            return element.value?.trim().length > 0;
        } catch (e) {
            return false;
        }
    }

    isJobApplicationField(label) {
        const keywords = [
            'name',
            'email',
            'phone',
            'experience',
            'skills',
            'motivation',
            'interest',
            'background',
            'question',
            'why',
            'tell',
            'describe',
            'cover',
            'letter',
            'additional',
            'comment',
            'years',
            'notice',
            'salary',
            'ctc',
            'expected',
        ];

        const lowerLabel = label.toLowerCase();
        return keywords.some((kw) => lowerLabel.includes(kw));
    }

    fillInputValue(element, value) {
        try {
            if (!element || !value) return;

            const descriptor = Object.getOwnPropertyDescriptor(
                Object.getPrototypeOf(element),
                'value'
            );

            if (descriptor?.set) {
                descriptor.set.call(element, value);
            } else {
                element.value = value;
            }

            element.dispatchEvent(new Event('input', { bubbles: true }));
            element.dispatchEvent(new Event('change', { bubbles: true }));
            element.dispatchEvent(new Event('blur', { bubbles: true }));

            if (element.contentEditable === 'true') {
                element.textContent = value;
                element.dispatchEvent(new Event('input', { bubbles: true }));
            }
        } catch (e) {
            console.error('Fill error:', e);
            try {
                element.value = value;
            } catch (fallbackError) {
                console.warn('Fallback fill failed:', fallbackError);
            }
        }
    }

    async handleStandardFields(label) {
        const standardMap = {
            email: () => this.userProfile.email,
            phone: () => this.userProfile.phone,
            'first name': () => this.userProfile.firstName,
            'last name': () => this.userProfile.lastName,
            'years of experience': () =>
                this.userProfile.yearsExperience?.toString(),
            'notice period': () => this.userProfile.noticePeriod,
            ctc: () => this.userProfile.ctc,
            'expected salary': () => this.userProfile.expectedSalary,
        };

        const lowerLabel = label.toLowerCase();
        for (const [key, getter] of Object.entries(standardMap)) {
            if (lowerLabel.includes(key)) {
                const value = getter();
                if (value) return value;
            }
        }

        return null;
    }

    async generateAnswer(label) {
        const standard = await this.handleStandardFields(label);
        if (standard) return standard;

        if (!this.gemini || !this.resumeText) {
            throw new Error('API or resume not configured');
        }

        return await this.gemini.generateAnswer(label, this.resumeText);
    }

    injectFieldButton(element, label) {
        try {
            const container = element.parentElement;
            if (!container) return;

            const btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'ai-field-btn';
            btn.textContent = '✨';
            btn.title = 'AI Fill this field';

            btn.style.cssText = `
                margin-left: 4px;
                padding: 4px 6px;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                color: white;
                border: none;
                border-radius: 3px;
                font-size: 12px;
                cursor: pointer;
                transition: all 0.2s;
                white-space: nowrap;
            `;

            btn.onmouseover = () => (btn.style.transform = 'scale(1.05)');
            btn.onmouseout = () => (btn.style.transform = 'scale(1)');

            btn.onclick = async (e) => {
                e.preventDefault();
                await this.fillField(element, label, btn);
            };

            container.insertBefore(btn, element.nextSibling);
        } catch (e) {
            console.warn('Field button injection failed:', e);
        }
    }

    async fillField(element, label, button) {
        if (!this.validateConfiguration()) return;

        const originalText = button.textContent;
        button.disabled = true;

        try {
            button.textContent = '⏳';
            this.showStatusToast(`Filling: ${label.substring(0, 30)}...`, 'loading', 0);

            const answer = await this.generateAnswer(label);
            this.fillInputValue(element, answer);
            button.textContent = '✓';
            this.showStatusToast(`✓ Filled: ${label.substring(0, 30)}...`, 'success', 3000);

            setTimeout(() => {
                button.textContent = originalText;
                button.disabled = false;
            }, 1500);
        } catch (error) {
            console.error('Fill error:', error);
            button.textContent = '❌';

            const errorMsg = error.message.includes('API')
                ? '⚠️ API Error: Rate limited or network issue'
                : '⚠️ Failed to generate answer';

            this.showStatusToast(errorMsg, 'error', 5000);

            setTimeout(() => {
                button.textContent = originalText;
                button.disabled = false;
            }, 2000);
        }
    }

    injectFormLevelButton(form, fields) {
        try {
            if (document.querySelector('.ai-form-fab')) return;

            const fab = document.createElement('div');
            fab.className = 'ai-form-fab';

            fab.style.cssText = `
                position: fixed;
                bottom: 24px;
                right: 24px;
                z-index: 9999;
            `;

            const button = document.createElement('button');
            button.type = 'button';
            button.textContent = '⚡ Fill All';
            button.className = 'ai-fab-btn';

            button.style.cssText = `
                padding: 12px 18px;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                color: white;
                border: none;
                border-radius: 24px;
                font-weight: 600;
                font-size: 14px;
                cursor: pointer;
                box-shadow: 0 4px 12px rgba(102, 126, 234, 0.4);
                transition: all 0.3s;
            `;

            button.onmouseover = () => {
                button.style.transform = 'translateY(-2px)';
                button.style.boxShadow = '0 6px 16px rgba(102, 126, 234, 0.6)';
            };

            button.onmouseout = () => {
                button.style.transform = 'translateY(0)';
                button.style.boxShadow = '0 4px 12px rgba(102, 126, 234, 0.4)';
            };

            button.onclick = async () => {
                await this.fillAllFields(fields, button);
            };

            fab.appendChild(button);
            document.body.appendChild(fab);
        } catch (e) {
            console.warn('FAB injection failed:', e);
        }
    }

    async fillAllFields(fields, button) {
        if (this.isProcessing) return;

        if (!this.validateConfiguration()) {
            this.showStatusToast(
                '⚠️ Cannot fill: Missing API key or resume',
                'error',
                5000
            );
            return;
        }

        const originalText = button.textContent;
        this.isProcessing = true;
        let filled = 0;
        let failed = 0;

        this.showStatusToast(
            `Starting form autofill (${fields.length} fields)...`,
            'loading',
            0
        );

        for (let i = 0; i < fields.length; i++) {
            const field = fields[i];

            try {
                if (this.isFieldFilled(field.element)) {
                    this.showStatusToast(
                        `Analyzing Form... ${i + 1}/${fields.length}`,
                        'loading',
                        0
                    );
                    continue;
                }

                this.showStatusToast(
                    `Filling field ${i + 1} of ${fields.length}...`,
                    'loading',
                    0
                );

                const answer = await this.generateAnswer(field.label);
                this.fillInputValue(field.element, answer);
                filled++;

                button.textContent = `⚡ ${filled}/${fields.length}`;

                await new Promise((resolve) => setTimeout(resolve, 400));
            } catch (error) {
                console.error(`Error filling "${field.label}":`, error);
                failed++;

                if (error.message.includes('429') || error.message.includes('rate')) {
                    this.showStatusToast(
                        '⚠️ Rate limited. Please wait before trying again.',
                        'warning',
                        4000
                    );
                    break;
                }
            }
        }

        this.showStatusToast(
            `✓ Form Autofilled Successfully! (${filled}/${fields.length} filled)`,
            'success',
            4000
        );

        button.textContent = '✓ Done';
        setTimeout(() => {
            button.textContent = originalText;
            this.isProcessing = false;
        }, 2000);
    }
}

if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => {
        new UniversalATSFormEngine();
    });
} else {
    new UniversalATSFormEngine();
}
