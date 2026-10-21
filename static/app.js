(() => {
  'use strict';

  const SORTS = new Set([
    'date_desc',
    'date_asc',
    'name_asc',
    'iso_asc',
    'iso_desc',
    'aperture_asc',
    'focal_asc',
  ]);

  const QUERY_FIELDS = [
    ['q', 'q'],
    ['camera', 'camera'],
    ['lens', 'lens'],
    ['format', 'format'],
    ['dateFrom', 'date_from'],
    ['dateTo', 'date_to'],
    ['isoMin', 'iso_min'],
    ['isoMax', 'iso_max'],
  ];

  function isPresent(value) {
    return value !== undefined && value !== null && value !== '';
  }

  function buildPhotoQuery(filters = {}) {
    const pairs = [];
    for (const [source, target] of QUERY_FIELDS) {
      const value = filters[source];
      if (!isPresent(value) || (typeof value === 'string' && value.trim() === '')) {
        continue;
      }
      pairs.push(`${target}=${encodeURIComponent(String(value).trim())}`);
    }

    const sort = SORTS.has(filters.sort) ? filters.sort : 'date_desc';
    pairs.push(`sort=${encodeURIComponent(sort)}`);

    if (isPresent(filters.offset)) {
      pairs.push(`offset=${encodeURIComponent(String(filters.offset))}`);
    }
    if (isPresent(filters.limit)) {
      pairs.push(`limit=${encodeURIComponent(String(filters.limit))}`);
    }

    return pairs.join('&');
  }

  function formatBytes(value) {
    if (value === null || value === undefined || value === '') {
      return '—';
    }
    const bytes = Number(value);
    if (!Number.isFinite(bytes) || bytes < 0) {
      return '—';
    }
    if (bytes < 1000) {
      return `${Math.round(bytes)} o`;
    }
    const units = ['Ko', 'Mo', 'Go', 'To'];
    let unitIndex = -1;
    let amount = bytes;
    while (amount >= 1000 && unitIndex < units.length - 1) {
      amount /= 1000;
      unitIndex += 1;
    }
    const digits = amount >= 100 ? 0 : amount >= 10 ? 1 : 1;
    return `${amount.toLocaleString('fr-FR', { maximumFractionDigits: digits })} ${units[unitIndex]}`;
  }

  function getPhotoAspectRatio(photo = {}) {
    const width = Number(photo.width);
    const height = Number(photo.height);
    if (Number.isFinite(width) && width > 0 && Number.isFinite(height) && height > 0) {
      return width / height;
    }
    return 1.5;
  }

  function formatExifValue(value) {
    if (value === undefined || value === null || value === '') {
      return '—';
    }
    return String(value);
  }

  function firstExifValue(values) {
    return values.find((value) => isPresent(value));
  }

  function formatCaptureDate(value) {
    if (!isPresent(value)) {
      return '';
    }
    const text = String(value).trim();
    const normalised = text.replace(/^(\\d{4}):(\\d{2}):(\\d{2})(?:\\s|$)/, '$1-$2-$3T');
    const date = new Date(normalised);
    if (Number.isNaN(date.getTime())) {
      return text;
    }
    return new Intl.DateTimeFormat('fr-FR', {
      dateStyle: 'medium',
      timeStyle: 'short',
    }).format(date);
  }

  function formatExifNumber(value, suffix = '') {
    if (!isPresent(value)) {
      return '';
    }
    const number = Number(value);
    if (!Number.isFinite(number)) {
      return String(value);
    }
    const formatted = Number.isInteger(number) ? String(number) : String(Number(number.toFixed(2)));
    return suffix ? `${formatted} ${suffix}` : formatted;
  }

  function formatAperture(value) {
    if (!isPresent(value)) {
      return '';
    }
    const number = Number(value);
    return Number.isFinite(number) ? `f/${Number.isInteger(number) ? number : Number(number.toFixed(1))}` : String(value);
  }

  function formatShutterSpeed(value) {
    if (!isPresent(value)) {
      return '';
    }
    if (typeof value === 'number' && Number.isFinite(value)) {
      if (value > 0 && value < 1) {
        return `1/${Math.round(1 / value)} s`;
      }
      return `${Number.isInteger(value) ? value : Number(value.toFixed(2))} s`;
    }
    return `${String(value)} s`;
  }

  function buildExifRows(photo = {}, detail = {}) {
    const merged = { ...photo, ...(detail && typeof detail === 'object' ? detail : {}) };
    const exif = detail && detail.exif && typeof detail.exif === 'object' ? detail.exif : {};
    const rows = [];
    const add = (label, value, formatter = formatExifValue) => {
      if (!isPresent(value)) {
        return;
      }
      const formatted = formatter(value);
      if (formatted) {
        rows.push({ label, value: formatted });
      }
    };

    add('Prise de vue', firstExifValue([
      merged.date,
      exif.DateTimeOriginal,
      exif.CreateDate,
      exif.ModifyDate,
    ]), formatCaptureDate);
    add('Appareil', firstExifValue([merged.camera, exif.Model, exif.Make]));
    add('Objectif', firstExifValue([merged.lens, exif.LensModel, exif.Lens]));
    add('Focale', firstExifValue([merged.focal, exif.FocalLength]), (value) => formatExifNumber(value, 'mm'));
    add('Ouverture', firstExifValue([merged.aperture, exif.FNumber, exif.Aperture]), formatAperture);
    add('Vitesse', firstExifValue([
      merged.shutter,
      exif.ShutterSpeed,
      exif.ExposureTime,
      exif.ShutterSpeedValue,
    ]), formatShutterSpeed);
    add('ISO', firstExifValue([merged.iso, exif.ISO]), formatExifNumber);
    const width = firstExifValue([merged.width, exif.ImageWidth, exif.ExifImageWidth]);
    const height = firstExifValue([merged.height, exif.ImageHeight, exif.ExifImageHeight]);
    if (isPresent(width) && isPresent(height)) {
      add('Dimensions', `${width} × ${height} px`);
    }
    add('Taille', firstExifValue([merged.size, exif.FileSize]), formatBytes);
    add('Format', firstExifValue([merged.format, exif.FileType, exif.FileTypeExtension]));
    const exposureCompensation = firstExifValue([
      merged.exposureCompensation,
      exif.ExposureCompensation,
    ]);
    if (isPresent(exposureCompensation)) {
      add('Correction d’exposition', exposureCompensation, (value) => {
        const number = Number(value);
        if (!Number.isFinite(number)) {
          return '';
        }
        const formatted = Number.isInteger(number) ? String(number) : String(Number(number.toFixed(2)));
        return `${number > 0 ? '+' : ''}${formatted} EV`;
      });
    }
    return rows;
  }

  function sessionPresentation(session = {}) {
    const passwordFree = session?.auth_required === false;
    return {
      label: passwordFree ? 'Accès Tailscale' : 'connecté',
      hideLogout: passwordFree,
    };
  }

  function formatDate(value, options = {}) {
    if (!value) {
      return '—';
    }
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) {
      return String(value);
    }
    return new Intl.DateTimeFormat('fr-FR', {
      day: '2-digit',
      month: options.short ? 'short' : 'long',
      year: 'numeric',
    }).format(date);
  }

  function finiteNumber(value) {
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
  }

  function normaliseAssetUrl(value) {
    if (typeof value !== 'string' || value.trim() === '') {
      return '';
    }
    try {
      const url = new URL(value, window.location.origin);
      if (url.protocol !== 'http:' && url.protocol !== 'https:') {
        return '';
      }
      return url.href;
    } catch {
      return '';
    }
  }

  const helpers = {
    buildPhotoQuery,
    formatBytes,
    getPhotoAspectRatio,
    formatExifValue,
    formatDate,
    sessionPresentation,
    buildExifRows,
  };

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = helpers;
  }

  if (typeof document === 'undefined') {
    return;
  }

  const PAGE_SIZE = 24;
  const POLL_INTERVAL = 1800;
  const state = {
    authenticated: false,
    sessionLabel: 'connecté',
    items: [],
    total: null,
    offset: 0,
    hasMore: false,
    photosRequest: null,
    photosSequence: 0,
    facetsRequest: null,
    facetsSequence: 0,
    statusRequest: null,
    statusSequence: 0,
    statusTimer: null,
    statusRunning: null,
    refreshAfterScan: false,
    searchTimer: null,
    lightboxIndex: -1,
    lightboxRequest: null,
    lightboxSequence: 0,
    previousFocus: null,
    swipeStartX: null,
    galleryLayoutFrame: null,
    galleryLayoutWidth: null,
  };

  const dom = {
    loginView: document.getElementById('login-view'),
    appView: document.getElementById('app-view'),
    loginForm: document.getElementById('login-form'),
    loginPassword: document.getElementById('login-password'),
    loginSubmit: document.getElementById('login-submit'),
    loginError: document.getElementById('login-error'),
    sessionStatus: document.getElementById('session-status'),
    scanButton: document.getElementById('scan-button'),
    logoutButton: document.getElementById('logout-button'),
    scanStatus: document.getElementById('scan-status'),
    scanStatusText: document.getElementById('scan-status-text'),
    uploadForm: document.getElementById('upload-form'),
    uploadInput: document.getElementById('upload-input'),
    uploadToggle: document.getElementById('upload-toggle'),
    uploadPanel: document.getElementById('upload-panel'),
    uploadDropzone: document.getElementById('upload-dropzone'),
    uploadChoose: document.getElementById('upload-choose'),
    uploadSubmit: document.getElementById('upload-submit'),
    uploadSummary: document.getElementById('upload-summary'),
    uploadErrors: document.getElementById('upload-errors'),
    searchInput: document.getElementById('search-input'),
    sortSelect: document.getElementById('sort-select'),
    gridSize: document.getElementById('grid-size'),
    gridSizeValue: document.getElementById('grid-size-value'),
    filtersForm: document.getElementById('filters-form'),
    filterDetails: document.getElementById('filter-details'),
    filterCount: document.getElementById('filter-count'),
    cameraSelect: document.getElementById('camera-filter'),
    lensSelect: document.getElementById('lens-filter'),
    formatSelect: document.getElementById('format-filter'),
    dateFrom: document.getElementById('date-from'),
    dateTo: document.getElementById('date-to'),
    isoMin: document.getElementById('iso-min'),
    isoMax: document.getElementById('iso-max'),
    resultsMeta: document.getElementById('results-meta'),
    resultCount: document.getElementById('result-count'),
    gallery: document.getElementById('gallery-grid'),
    galleryLoading: document.getElementById('gallery-loading'),
    galleryEmpty: document.getElementById('gallery-empty'),
    galleryError: document.getElementById('gallery-error'),
    galleryErrorText: document.getElementById('gallery-error-text'),
    loadMore: document.getElementById('load-more'),
    lightbox: document.getElementById('lightbox'),
    lightboxTitle: document.getElementById('lightbox-title'),
    lightboxPosition: document.getElementById('lightbox-position'),
    lightboxImage: document.getElementById('lightbox-image'),
    lightboxImageLoader: document.getElementById('lightbox-image-loader'),
    lightboxImageFrame: document.getElementById('lightbox-image-frame'),
    lightboxDownload: document.getElementById('lightbox-download'),
    lightboxExif: document.getElementById('lightbox-exif-list'),
    lightboxLoading: document.getElementById('lightbox-loading'),
    lightboxError: document.getElementById('lightbox-error'),
    lightboxPrevious: document.getElementById('lightbox-previous'),
    lightboxNext: document.getElementById('lightbox-next'),
    lightboxClose: document.getElementById('lightbox-close'),
  };

  class ApiError extends Error {
    constructor(message, status = 0) {
      super(message);
      this.name = 'ApiError';
      this.status = status;
    }
  }

  function setHidden(element, hidden) {
    if (!element) {
      return;
    }
    element.hidden = hidden;
    element.setAttribute('aria-hidden', String(hidden));
  }

  function setMessage(element, message, kind = '') {
    if (!element) {
      return;
    }
    if (element === dom.scanStatus && dom.scanStatusText) {
      dom.scanStatusText.textContent = message || '';
    } else {
      element.textContent = message || '';
    }
    element.classList.toggle('is-error', kind === 'error');
    element.classList.toggle('is-success', kind === 'success');
    element.classList.toggle('is-info', kind === 'info');
    setHidden(element, !message);
  }

  function textElement(tagName, className, text) {
    const element = document.createElement(tagName);
    if (className) {
      element.className = className;
    }
    if (text !== undefined) {
      element.textContent = text;
    }
    return element;
  }

  function displayErrorMessage(error, fallback = 'Une erreur est survenue. Réessayez.') {
    if (error instanceof ApiError && error.message) {
      return error.message;
    }
    if (error instanceof TypeError && error.message === 'Failed to fetch') {
      return 'Serveur indisponible. Vérifiez la connexion.';
    }
    return fallback;
  }

  async function fetchJson(url, options = {}) {
    const requestOptions = { ...options };
    requestOptions.credentials = 'same-origin';
    requestOptions.headers = {
      Accept: 'application/json',
      ...(options.headers || {}),
    };

    const response = await fetch(url, requestOptions);
    const raw = await response.text();
    let data = null;
    if (raw) {
      try {
        data = JSON.parse(raw);
      } catch {
        data = null;
      }
    }
    if (!response.ok) {
      const message = data && typeof data === 'object'
        ? data.error || data.detail || data.message
        : null;
      throw new ApiError(message || `Requête impossible (${response.status}).`, response.status);
    }
    return data;
  }

  function cancelPendingRequests() {
    [state.photosRequest, state.facetsRequest, state.statusRequest, state.lightboxRequest]
      .filter(Boolean)
      .forEach((controller) => controller.abort());
    state.photosSequence += 1;
    state.facetsSequence += 1;
    state.statusSequence += 1;
    state.lightboxSequence += 1;
  }

  function showLogin(message = '') {
    cancelPendingRequests();
    state.authenticated = false;
    state.sessionLabel = 'connecté';
    stopStatusPolling();
    if (dom.lightbox.open || dom.lightbox.hasAttribute('open')) {
      closeLightbox();
    }
    setHidden(dom.appView, true);
    setHidden(dom.loginView, false);
    setHidden(dom.logoutButton, false);
    setMessage(dom.loginError, message, message ? 'error' : '');
    window.setTimeout(() => dom.loginPassword?.focus(), 0);
  }

  function showApp(session = {}) {
    const presentation = sessionPresentation(session);
    state.authenticated = true;
    state.sessionLabel = presentation.label;
    setHidden(dom.loginView, true);
    setHidden(dom.appView, false);
    setHidden(dom.logoutButton, presentation.hideLogout);
    setMessage(dom.loginError, '');
    setUploadExpanded(false);
    dom.galleryErrorText.textContent = '';
    dom.sessionStatus.textContent = state.sessionLabel;
    dom.sessionStatus.classList.add('is-connected');
    void loadFacets();
    void loadPhotos({ reset: true });
    void refreshStatus();
  }

  function currentFilters() {
    return {
      q: dom.searchInput.value,
      camera: dom.cameraSelect.value,
      lens: dom.lensSelect.value,
      format: dom.formatSelect.value,
      dateFrom: dom.dateFrom.value,
      dateTo: dom.dateTo.value,
      isoMin: dom.isoMin.value,
      isoMax: dom.isoMax.value,
      sort: dom.sortSelect.value,
      offset: state.offset,
      limit: PAGE_SIZE,
    };
  }

  function updateFilterCount() {
    const values = [
      dom.cameraSelect.value,
      dom.lensSelect.value,
      dom.formatSelect.value,
      dom.dateFrom.value,
      dom.dateTo.value,
      dom.isoMin.value,
      dom.isoMax.value,
    ];
    const count = values.filter(Boolean).length;
    dom.filterCount.textContent = count ? `${count} actif${count > 1 ? 's' : ''}` : 'aucun filtre';
  }

  function showGalleryState(stateName) {
    setHidden(dom.galleryLoading, stateName !== 'loading');
    setHidden(dom.galleryEmpty, stateName !== 'empty');
    setHidden(dom.galleryError, stateName !== 'error');
    setHidden(dom.gallery, stateName !== 'grid');
    if (stateName === 'grid') {
      dom.gallery.removeAttribute('aria-busy');
    } else if (stateName === 'loading') {
      dom.gallery.setAttribute('aria-busy', 'true');
    }
  }

  function updateGalleryMasonry() {
    if (!dom.gallery || dom.gallery.hidden) {
      return;
    }
    const cards = [...dom.gallery.querySelectorAll('.photo-card')];
    if (!cards.length) {
      return;
    }
    const gridStyle = window.getComputedStyle(dom.gallery);
    const rowHeight = Number.parseFloat(gridStyle.gridAutoRows);
    const rowGap = Number.parseFloat(gridStyle.rowGap);
    if (!Number.isFinite(rowHeight) || rowHeight <= 0 || !Number.isFinite(rowGap)) {
      return;
    }
    state.galleryLayoutWidth = dom.gallery.getBoundingClientRect().width;

    // The grid item remains allocated while its intrinsic content is measured.
    // Resetting every span first collapses document height and clamps scrollY.
    const measurements = cards.map((card) => {
      const height = card.getBoundingClientRect().height;
      const span = Math.max(1, Math.ceil((height + rowGap) / (rowHeight + rowGap)));
      return { card, span };
    });

    // Keep all layout reads above the writes, and avoid touching unchanged spans.
    measurements.forEach(({ card, span }) => {
      const next = `span ${span}`;
      if (card.style.gridRowEnd !== next) {
        card.style.gridRowEnd = next;
      }
    });
  }

  function scheduleGalleryLayout() {
    if (state.galleryLayoutFrame !== null) {
      window.cancelAnimationFrame(state.galleryLayoutFrame);
    }
    state.galleryLayoutFrame = window.requestAnimationFrame(() => {
      state.galleryLayoutFrame = null;
      updateGalleryMasonry();
    });
  }

  function formatCount(count, singular, plural = `${singular}s`) {
    return `${count} ${count === 1 ? singular : plural}`;
  }

  function updateResultsMeta() {
    const loadedCount = state.items.length;
    if (state.total === null) {
      dom.resultCount.textContent = loadedCount ? formatCount(loadedCount, 'photo') : '—';
    } else {
      dom.resultCount.textContent = `${loadedCount} sur ${state.total} ${state.total === 1 ? 'photo' : 'photos'}`;
    }
    dom.resultsMeta.classList.toggle('has-results', loadedCount > 0);
    const canLoadMore = state.total !== null
      ? loadedCount < state.total
      : state.hasMore;
    setHidden(dom.loadMore, !canLoadMore);
    dom.loadMore.disabled = false;
  }

  function appendPhotoMetadata(parent, photo) {
    const metadata = textElement('div', 'photo-card__metadata');
    const format = formatExifValue(photo.format).toUpperCase();
    const date = formatDate(photo.date, { short: true });
    if (format !== '—') {
      metadata.append(textElement('span', 'metadata-label', format));
    }
    if (date !== '—') {
      metadata.append(textElement('span', '', date));
    }
    parent.append(metadata);
  }

  function makeDownloadLink(photo, className = '') {
    const link = textElement('a', `photo-download ${className}`.trim(), 'Télécharger');
    const url = normaliseAssetUrl(photo.download_url);
    if (url) {
      link.href = url;
      link.setAttribute('download', photo.name || 'photo');
      link.setAttribute('aria-label', `Télécharger ${photo.name || 'la photo'}`);
    } else {
      link.setAttribute('aria-disabled', 'true');
      link.tabIndex = -1;
      link.classList.add('is-disabled');
    }
    return link;
  }

  function makePhotoCard(photo, index) {
    const card = textElement('article', 'photo-card');
    card.dataset.photoId = photo.id === undefined || photo.id === null ? '' : String(photo.id);

    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'photo-card__open';
    button.setAttribute('aria-label', `Ouvrir ${photo.name || 'la photo'}`);
    button.addEventListener('click', () => openLightbox(index));

    const frame = textElement('span', 'photo-card__frame');
    const assetUrl = normaliseAssetUrl(photo.thumb_url || photo.preview_url);
    const width = finiteNumber(photo.width);
    const height = finiteNumber(photo.height);
    frame.style.aspectRatio = `${getPhotoAspectRatio(photo)}`;
    if (assetUrl) {
      const image = document.createElement('img');
      image.className = 'photo-card__image';
      image.alt = photo.name || 'Photo';
      image.loading = 'lazy';
      image.decoding = 'async';
      if (width && height) {
        image.width = width;
        image.height = height;
      }
      frame.classList.add('is-loading');
      const clearLoading = () => {
        frame.classList.remove('is-loading');
        scheduleGalleryLayout();
      };
      image.addEventListener('load', clearLoading, { once: true });
      image.addEventListener('error', () => {
        clearLoading();
        image.hidden = true;
        frame.classList.add('is-missing');
        const fallback = textElement('span', 'photo-card__missing', 'Aperçu indisponible');
        frame.append(fallback);
      }, { once: true });
      frame.append(image);
      image.src = assetUrl;
      if (image.complete && image.naturalWidth > 0) {
        clearLoading();
      }
    } else {
      frame.classList.add('is-missing');
      frame.append(textElement('span', 'photo-card__missing', 'Aperçu indisponible'));
    }

    const overlay = textElement('span', 'photo-card__overlay');
    overlay.append(textElement('span', 'photo-card__index', String(index + 1).padStart(2, '0')));
    overlay.append(textElement('span', 'photo-card__open-hint', 'Voir'));
    frame.append(overlay);
    button.append(frame);
    card.append(button);

    const footer = textElement('div', 'photo-card__footer');
    const caption = textElement('div', 'photo-card__caption');
    caption.append(textElement('h3', 'photo-card__name', formatExifValue(photo.name)));
    appendPhotoMetadata(caption, photo);
    footer.append(caption);
    footer.append(makeDownloadLink(photo));
    card.append(footer);
    return card;
  }

  function renderGallery({ reset = true } = {}) {
    const existingCount = reset ? 0 : dom.gallery.querySelectorAll('.photo-card').length;
    if (reset) {
      dom.gallery.replaceChildren();
    }
    state.items.slice(existingCount).forEach((photo, index) => {
      dom.gallery.append(makePhotoCard(photo, index + existingCount));
    });
    updateResultsMeta();
    if (state.items.length === 0) {
      showGalleryState('empty');
    } else {
      showGalleryState('grid');
      scheduleGalleryLayout();
    }
  }

  function renderGalleryError(message) {
    dom.galleryErrorText.textContent = message;
    showGalleryState('error');
    updateResultsMeta();
  }

  async function loadPhotos({ reset = true } = {}) {
    if (state.photosRequest) {
      state.photosRequest.abort();
    }
    const controller = new AbortController();
    state.photosRequest = controller;
    const sequence = ++state.photosSequence;
    if (reset) {
      state.offset = 0;
      state.items = [];
      state.total = null;
      showGalleryState('loading');
      updateResultsMeta();
    }

    try {
      const query = buildPhotoQuery(currentFilters());
      const payload = await fetchJson(`/api/photos?${query}`, { signal: controller.signal });
      if (sequence !== state.photosSequence || controller.signal.aborted) {
        return;
      }
      const incoming = Array.isArray(payload?.items) ? payload.items : [];
      const reportedTotal = finiteNumber(payload?.total);
      state.items = reset ? incoming : state.items.concat(incoming);
      state.total = reportedTotal === null ? null : reportedTotal;
      state.offset = state.items.length;
      state.hasMore = incoming.length >= PAGE_SIZE;
      renderGallery({ reset });
    } catch (error) {
      if (controller.signal.aborted || sequence !== state.photosSequence) {
        return;
      }
      if (reset) {
        state.items = [];
        state.total = null;
      }
      renderGalleryError(displayErrorMessage(error, 'Les photos ne peuvent pas être chargées pour le moment.'));
    } finally {
      if (state.photosRequest === controller) {
        state.photosRequest = null;
      }
    }
  }

  function facetValue(value) {
    if (typeof value === 'string' || typeof value === 'number') {
      return String(value);
    }
    if (value && typeof value === 'object') {
      return String(value.name ?? value.value ?? value.label ?? '');
    }
    return '';
  }

  function fillSelect(select, values, placeholder) {
    const previous = select.value;
    select.replaceChildren();
    const emptyOption = document.createElement('option');
    emptyOption.value = '';
    emptyOption.textContent = placeholder;
    select.append(emptyOption);
    const uniqueValues = [...new Set((Array.isArray(values) ? values : []).map(facetValue).filter(Boolean))]
      .sort((a, b) => a.localeCompare(b, 'fr', { sensitivity: 'base' }));
    uniqueValues.forEach((value) => {
      const option = document.createElement('option');
      option.value = value;
      option.textContent = value;
      select.append(option);
    });
    if (uniqueValues.includes(previous)) {
      select.value = previous;
    }
  }

  async function loadFacets() {
    if (state.facetsRequest) {
      state.facetsRequest.abort();
    }
    const controller = new AbortController();
    state.facetsRequest = controller;
    const sequence = ++state.facetsSequence;
    try {
      const payload = await fetchJson('/api/facets', { signal: controller.signal });
      if (sequence !== state.facetsSequence || controller.signal.aborted) {
        return;
      }
      fillSelect(dom.cameraSelect, payload?.cameras, 'Tous les appareils');
      fillSelect(dom.lensSelect, payload?.lenses, 'Tous les objectifs');
      fillSelect(dom.formatSelect, payload?.formats, 'Tous les formats');
      updateFilterCount();
    } catch (error) {
      if (!controller.signal.aborted && sequence === state.facetsSequence) {
        // Facets are optional for a usable gallery; keep the controls available.
        setMessage(dom.scanStatus, displayErrorMessage(error, 'Les filtres détaillés sont indisponibles.'), 'error');
      }
    } finally {
      if (state.facetsRequest === controller) {
        state.facetsRequest = null;
      }
    }
  }

  function renderStatus(payload) {
    const running = Boolean(payload?.running);
    const processed = finiteNumber(payload?.processed);
    const total = finiteNumber(payload?.total);
    if (running) {
      let message = 'Import en cours';
      if (processed !== null && total !== null) {
        message += ` · ${processed} / ${total}`;
      } else if (processed !== null) {
        message += ` · ${processed} traité${processed === 1 ? '' : 's'}`;
      }
      setMessage(dom.scanStatus, message, 'info');
      dom.sessionStatus.textContent = 'import en cours';
      dom.sessionStatus.classList.remove('is-connected');
    } else {
      dom.sessionStatus.textContent = state.sessionLabel;
      dom.sessionStatus.classList.add('is-connected');
      if (payload?.errors) {
        const errors = Array.isArray(payload.errors) ? payload.errors.length : finiteNumber(payload.errors);
        if (errors) {
          setMessage(dom.scanStatus, `${errors} erreur${errors === 1 ? '' : 's'} pendant le dernier import.`, 'error');
        } else if (!dom.scanStatus.classList.contains('is-error')) {
          setMessage(dom.scanStatus, '');
        }
      } else if (!dom.scanStatus.classList.contains('is-error')) {
        setMessage(dom.scanStatus, '');
      }
    }
  }

  function stopStatusPolling() {
    if (state.statusTimer !== null) {
      window.clearTimeout(state.statusTimer);
      state.statusTimer = null;
    }
  }

  function scheduleStatusPoll() {
    stopStatusPolling();
    if (!state.authenticated || !state.statusRunning) {
      return;
    }
    state.statusTimer = window.setTimeout(() => {
      state.statusTimer = null;
      void refreshStatus({ poll: true });
    }, POLL_INTERVAL);
  }

  async function refreshStatus({ poll = false } = {}) {
    if (!state.authenticated) {
      return;
    }
    if (state.statusRequest) {
      state.statusRequest.abort();
    }
    const controller = new AbortController();
    state.statusRequest = controller;
    const sequence = ++state.statusSequence;
    try {
      const payload = await fetchJson('/api/status', { signal: controller.signal });
      if (sequence !== state.statusSequence || controller.signal.aborted) {
        return;
      }
      const wasRunning = state.statusRunning === true;
      state.statusRunning = Boolean(payload?.running);
      renderStatus(payload || {});
      if (state.statusRunning) {
        scheduleStatusPoll();
      } else {
        stopStatusPolling();
        if (wasRunning || state.refreshAfterScan) {
          state.refreshAfterScan = false;
          await refreshLibrary();
        }
      }
    } catch (error) {
      if (!controller.signal.aborted && sequence === state.statusSequence && !poll) {
        setMessage(dom.scanStatus, displayErrorMessage(error, 'Le statut du serveur est indisponible.'), 'error');
      }
    } finally {
      if (state.statusRequest === controller) {
        state.statusRequest = null;
      }
    }
  }

  async function refreshLibrary() {
    await Promise.allSettled([
      loadPhotos({ reset: true }),
      loadFacets(),
    ]);
  }

  async function triggerScan() {
    if (dom.scanButton.disabled) {
      return;
    }
    dom.scanButton.disabled = true;
    state.refreshAfterScan = true;
    setMessage(dom.scanStatus, 'Préparation de l’import…', 'info');
    try {
      await fetchJson('/api/scan', { method: 'POST' });
      await refreshStatus();
    } catch (error) {
      state.refreshAfterScan = false;
      setMessage(dom.scanStatus, displayErrorMessage(error, 'Le scan n’a pas pu démarrer.'), 'error');
    } finally {
      dom.scanButton.disabled = false;
    }
  }

  function setUploadExpanded(expanded) {
    const isExpanded = Boolean(expanded);
    if (!dom.uploadToggle || !dom.uploadPanel) {
      return;
    }
    dom.uploadToggle.setAttribute('aria-expanded', String(isExpanded));
    dom.uploadPanel.hidden = !isExpanded;
    dom.uploadToggle.closest('.upload-section')?.classList.toggle('is-open', isExpanded);
  }

  function clearUploadMessages() {
    setMessage(dom.uploadSummary, '');
    dom.uploadErrors.replaceChildren();
    setHidden(dom.uploadErrors, true);
  }

  function errorValue(error) {
    if (typeof error === 'string') {
      return error;
    }
    if (error && typeof error === 'object') {
      return error.message || error.error || error.name || JSON.stringify(error);
    }
    return String(error ?? 'Erreur inconnue');
  }

  function renderUploadErrors(errors) {
    const list = Array.isArray(errors) ? errors : [];
    dom.uploadErrors.replaceChildren();
    if (!list.length) {
      setHidden(dom.uploadErrors, true);
      return;
    }
    list.forEach((error) => {
      dom.uploadErrors.append(textElement('li', '', errorValue(error)));
    });
    setHidden(dom.uploadErrors, false);
  }

  async function uploadFiles(fileList) {
    const files = Array.from(fileList || []);
    if (!files.length || dom.uploadSubmit.disabled) {
      return;
    }
    clearUploadMessages();
    dom.uploadSubmit.disabled = true;
    dom.uploadSubmit.textContent = 'Import…';
    const formData = new FormData();
    files.forEach((file) => formData.append('files', file, file.name));
    try {
      const payload = await fetchJson('/api/upload', {
        method: 'POST',
        body: formData,
      });
      const accepted = Array.isArray(payload?.accepted)
        ? payload.accepted.length
        : finiteNumber(payload?.accepted);
      const acceptedLabel = accepted === null ? 'Import terminé.' : `${accepted} fichier${accepted === 1 ? '' : 's'} accepté${accepted === 1 ? '' : 's'}.`;
      setMessage(dom.uploadSummary, acceptedLabel, accepted ? 'success' : 'info');
      renderUploadErrors(payload?.errors);
      state.refreshAfterScan = true;
      await refreshLibrary();
      await refreshStatus();
    } catch (error) {
      setMessage(dom.uploadSummary, displayErrorMessage(error, 'L’import n’a pas pu être envoyé.'), 'error');
    } finally {
      dom.uploadSubmit.disabled = false;
      dom.uploadSubmit.textContent = 'Importer';
      dom.uploadInput.value = '';
      dom.uploadDropzone.classList.remove('is-dragging');
    }
  }

  function updateGridSize() {
    const size = Number(dom.gridSize.value) || 220;
    document.documentElement.style.setProperty('--tile-size', `${size}px`);
    dom.gridSizeValue.textContent = `${size}px`;
    if (dom.gallery && !dom.gallery.hidden) {
      scheduleGalleryLayout();
    }
  }

  function scheduleSearch() {
    if (state.searchTimer !== null) {
      window.clearTimeout(state.searchTimer);
    }
    state.searchTimer = window.setTimeout(() => {
      state.searchTimer = null;
      void loadPhotos({ reset: true });
    }, 280);
  }

  function focusableInDialog() {
    return [...dom.lightbox.querySelectorAll('button:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])')]
      .filter((element) => !element.hidden && element.getAttribute('aria-hidden') !== 'true');
  }

  function renderExifRows(photo, detail) {
    const rows = buildExifRows(photo, detail);
    dom.lightboxExif.replaceChildren();
    rows.forEach(({ label, value }) => {
      const row = textElement('div', 'exif-row');
      row.append(textElement('dt', '', label));
      row.append(textElement('dd', '', value));
      dom.lightboxExif.append(row);
    });
  }

  function populateLightbox(photo, detail = null) {
    const title = formatExifValue(photo.name);
    dom.lightboxTitle.textContent = title;
    dom.lightboxPosition.textContent = `${state.lightboxIndex + 1} / ${state.items.length}`;
    const assetUrl = normaliseAssetUrl(photo.preview_url || photo.thumb_url || photo.download_url);
    dom.lightboxImage.hidden = !assetUrl;
    setHidden(dom.lightboxImageLoader, !assetUrl);
    dom.lightboxImageFrame.classList.toggle('is-missing', !assetUrl);
    if (assetUrl) {
      dom.lightboxImage.src = assetUrl;
      dom.lightboxImage.alt = title;
      const width = finiteNumber(photo.width);
      const height = finiteNumber(photo.height);
      if (width && height) {
        dom.lightboxImage.width = width;
        dom.lightboxImage.height = height;
      }
    } else {
      dom.lightboxImage.removeAttribute('src');
    }
    const downloadUrl = normaliseAssetUrl(photo.download_url);
    if (downloadUrl) {
      dom.lightboxDownload.href = downloadUrl;
      dom.lightboxDownload.setAttribute('download', photo.name || 'photo');
      dom.lightboxDownload.removeAttribute('aria-disabled');
      dom.lightboxDownload.classList.remove('is-disabled');
    } else {
      dom.lightboxDownload.removeAttribute('href');
      dom.lightboxDownload.setAttribute('aria-disabled', 'true');
      dom.lightboxDownload.classList.add('is-disabled');
    }
    dom.lightboxPrevious.disabled = state.lightboxIndex <= 0;
    dom.lightboxNext.disabled = state.lightboxIndex >= state.items.length - 1;
    renderExifRows(photo, detail || photo);
  }

  async function loadLightboxDetails(photo) {
    if (state.lightboxRequest) {
      state.lightboxRequest.abort();
    }
    const controller = new AbortController();
    state.lightboxRequest = controller;
    const sequence = ++state.lightboxSequence;
    setMessage(dom.lightboxError, '');
    setMessage(dom.lightboxLoading, 'Chargement des métadonnées…', 'info');
    try {
      const payload = await fetchJson(`/api/photos/${encodeURIComponent(String(photo.id))}`, { signal: controller.signal });
      if (sequence !== state.lightboxSequence || controller.signal.aborted) {
        return;
      }
      renderExifRows(photo, payload);
      setMessage(dom.lightboxLoading, '');
    } catch (error) {
      if (controller.signal.aborted || sequence !== state.lightboxSequence) {
        return;
      }
      setMessage(dom.lightboxLoading, '');
      setMessage(dom.lightboxError, displayErrorMessage(error, 'Métadonnées détaillées indisponibles.'), 'error');
    } finally {
      if (state.lightboxRequest === controller) {
        state.lightboxRequest = null;
      }
    }
  }

  function openLightbox(index) {
    if (!state.items[index]) {
      return;
    }
    state.previousFocus = document.activeElement;
    state.lightboxIndex = index;
    const photo = state.items[index];
    populateLightbox(photo);
    if (!dom.lightbox.open) {
      if (typeof dom.lightbox.showModal === 'function') {
        dom.lightbox.showModal();
      } else {
        dom.lightbox.setAttribute('open', '');
      }
      document.body.classList.add('modal-open');
    }
    void loadLightboxDetails(photo);
    window.setTimeout(() => dom.lightboxClose.focus({ preventScroll: true }), 0);
  }

  function navigateLightbox(direction) {
    const nextIndex = state.lightboxIndex + direction;
    if (nextIndex < 0 || nextIndex >= state.items.length) {
      return;
    }
    state.lightboxIndex = nextIndex;
    const photo = state.items[nextIndex];
    populateLightbox(photo);
    void loadLightboxDetails(photo);
  }

  function closeLightbox() {
    if (!dom.lightbox.open && !dom.lightbox.hasAttribute('open')) {
      return;
    }
    if (state.lightboxRequest) {
      state.lightboxRequest.abort();
      state.lightboxRequest = null;
    }
    state.lightboxSequence += 1;
    if (typeof dom.lightbox.close === 'function' && dom.lightbox.open) {
      dom.lightbox.close();
    } else {
      dom.lightbox.removeAttribute('open');
    }
    document.body.classList.remove('modal-open');
    if (state.previousFocus && typeof state.previousFocus.focus === 'function') {
      state.previousFocus.focus({ preventScroll: true });
    }
    state.previousFocus = null;
  }

  function onLightboxKeydown(event) {
    if (event.key === 'Escape') {
      event.preventDefault();
      closeLightbox();
      return;
    }
    if (event.key === 'ArrowLeft') {
      event.preventDefault();
      navigateLightbox(-1);
      return;
    }
    if (event.key === 'ArrowRight') {
      event.preventDefault();
      navigateLightbox(1);
      return;
    }
    if (event.key !== 'Tab') {
      return;
    }
    const focusable = focusableInDialog();
    if (!focusable.length) {
      event.preventDefault();
      return;
    }
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  function onLightboxPointerDown(event) {
    state.swipeStartX = event.clientX;
  }

  function onLightboxPointerUp(event) {
    if (state.swipeStartX === null) {
      return;
    }
    const distance = event.clientX - state.swipeStartX;
    state.swipeStartX = null;
    if (Math.abs(distance) < 48) {
      return;
    }
    navigateLightbox(distance < 0 ? 1 : -1);
  }

  async function submitLogin(event) {
    event.preventDefault();
    if (dom.loginSubmit.disabled) {
      return;
    }
    dom.loginSubmit.disabled = true;
    setMessage(dom.loginError, '');
    try {
      await fetchJson('/api/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ password: dom.loginPassword.value }),
      });
      dom.loginPassword.value = '';
      showApp();
    } catch (error) {
      setMessage(dom.loginError, displayErrorMessage(error, 'Mot de passe incorrect ou serveur indisponible.'), 'error');
      dom.loginPassword.select();
    } finally {
      dom.loginSubmit.disabled = false;
    }
  }

  async function logout() {
    if (dom.logoutButton.disabled) {
      return;
    }
    dom.logoutButton.disabled = true;
    try {
      await fetchJson('/api/logout', { method: 'POST' });
      showLogin();
    } catch (error) {
      setMessage(dom.scanStatus, displayErrorMessage(error, 'La déconnexion a échoué.'), 'error');
    } finally {
      dom.logoutButton.disabled = false;
    }
  }

  function onFilterChanged() {
    updateFilterCount();
    void loadPhotos({ reset: true });
  }

  function onViewportResize() {
    if (!dom.gallery || dom.gallery.hidden) {
      return;
    }
    const width = dom.gallery.getBoundingClientRect().width;
    if (!Number.isFinite(width) || width <= 0) {
      return;
    }
    if (state.galleryLayoutWidth !== null && Math.abs(width - state.galleryLayoutWidth) < 0.5) {
      return;
    }
    scheduleGalleryLayout();
  }

  function bindEvents() {
    dom.lightboxClose.textContent = 'Retour à la galerie';
    dom.lightboxClose.setAttribute('aria-label', 'Retour à la galerie');
    dom.loginForm.addEventListener('submit', submitLogin);
    dom.scanButton.addEventListener('click', triggerScan);
    dom.logoutButton.addEventListener('click', logout);

    dom.searchInput.addEventListener('input', scheduleSearch);
    dom.sortSelect.addEventListener('change', onFilterChanged);
    dom.filtersForm.addEventListener('change', onFilterChanged);
    dom.gridSize.addEventListener('input', updateGridSize);
    dom.loadMore.addEventListener('click', () => loadPhotos({ reset: false }));
    dom.uploadToggle.addEventListener('click', () => {
      setUploadExpanded(dom.uploadToggle.getAttribute('aria-expanded') !== 'true');
    });
    window.addEventListener('resize', onViewportResize);

    dom.uploadChoose.addEventListener('click', () => dom.uploadInput.click());
    dom.uploadDropzone.addEventListener('click', (event) => {
      if (event.target !== dom.uploadChoose) {
        dom.uploadInput.click();
      }
    });
    dom.uploadDropzone.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        dom.uploadInput.click();
      }
    });
    dom.uploadInput.addEventListener('change', () => uploadFiles(dom.uploadInput.files));
    dom.uploadForm.addEventListener('submit', (event) => {
      event.preventDefault();
      uploadFiles(dom.uploadInput.files);
    });
    ['dragenter', 'dragover'].forEach((eventName) => {
      dom.uploadDropzone.addEventListener(eventName, (event) => {
        event.preventDefault();
        dom.uploadDropzone.classList.add('is-dragging');
      });
    });
    ['dragleave', 'drop'].forEach((eventName) => {
      dom.uploadDropzone.addEventListener(eventName, (event) => {
        event.preventDefault();
        dom.uploadDropzone.classList.remove('is-dragging');
      });
    });
    dom.uploadDropzone.addEventListener('drop', (event) => uploadFiles(event.dataTransfer.files));

    dom.lightbox.addEventListener('keydown', onLightboxKeydown);
    dom.lightboxClose.addEventListener('click', closeLightbox);
    dom.lightboxPrevious.addEventListener('click', () => navigateLightbox(-1));
    dom.lightboxNext.addEventListener('click', () => navigateLightbox(1));
    dom.lightboxImage.addEventListener('load', () => {
      setHidden(dom.lightboxImageLoader, true);
    });
    dom.lightboxImage.addEventListener('error', () => {
      setHidden(dom.lightboxImageLoader, true);
      dom.lightboxImage.hidden = true;
      dom.lightboxImageFrame.classList.add('is-missing');
    });
    dom.lightboxImageFrame.addEventListener('pointerdown', onLightboxPointerDown);
    dom.lightboxImageFrame.addEventListener('pointerup', onLightboxPointerUp);
    dom.lightbox.addEventListener('cancel', (event) => {
      event.preventDefault();
      closeLightbox();
    });
    dom.lightbox.addEventListener('close', () => {
      document.body.classList.remove('modal-open');
    });
  }

  async function bootstrap() {
    bindEvents();
    updateGridSize();
    updateFilterCount();
    try {
      const session = await fetchJson('/api/session');
      if (session?.authenticated) {
        showApp(session);
      } else {
        showLogin();
      }
    } catch (error) {
      showLogin('Connexion au serveur impossible. Réessayez.');
    }
  }

  void bootstrap();
})();
