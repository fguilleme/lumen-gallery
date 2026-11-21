(() => {
  'use strict';

  const SORTS = new Set([
    'date_desc',
    'date_asc',
    'name_asc',
    'directory_asc',
    'directory_desc',
    'iso_asc',
    'iso_desc',
    'aperture_asc',
    'focal_asc',
    'rating_asc',
    'rating_desc',
  ]);

  const MEDIA_TYPES = new Set(['photo', 'video']);

  const SIMILARITY_MODES = new Set(['near', 'content']);
  const SIMILARITY_LABELS = Object.freeze({
    near: 'Photos proches',
    content: 'Contenu similaire',
  });

  const QUERY_FIELDS = [
    ['mediaType', 'media_type'],
    ['importedOnly', 'imported_only'],
    ['rating', 'rating'],
    ['deletionStatus', 'deletion_status'],
    ['q', 'q'],
    ['directory', 'directory'],
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

  function normaliseSimilarity(value) {
    const candidate = typeof value === 'string' ? value.trim() : value;
    return SIMILARITY_MODES.has(candidate) ? candidate : '';
  }

  function normaliseRating(value) {
    const number = typeof value === 'string' && value.trim() === '' ? NaN : Number(value);
    return Number.isInteger(number) && number >= 0 && number <= 4 ? number : 0;
  }

  function isRatingFilterValue(value) {
    if (!isPresent(value)) {
      return false;
    }
    const number = Number(value);
    return Number.isInteger(number) && number >= 0 && number <= 4;
  }

  function ratingValueFromPayload(payload, fallback) {
    const item = payload && typeof payload === 'object' && payload.item && typeof payload.item === 'object'
      ? payload.item
      : payload;
    if (item && Object.prototype.hasOwnProperty.call(item, 'rating') && isRatingFilterValue(item.rating)) {
      return normaliseRating(item.rating);
    }
    return normaliseRating(fallback);
  }

  function createRatingQueue(send, { onSettled = () => {} } = {}) {
    if (typeof send !== 'function') {
      throw new TypeError('createRatingQueue requires a send function');
    }
    const entries = new Map();

    function entryFor(id) {
      const key = String(id);
      let entry = entries.get(key);
      if (!entry) {
        entry = {
          id: key,
          pending: null,
          inFlight: null,
          sequence: 0,
          serverRating: 0,
        };
        entries.set(key, entry);
      }
      return entry;
    }

    function notify(event) {
      try {
        onSettled(event);
      } catch {
        // A UI callback must never break per-id persistence.
      }
    }

    async function drain(entry) {
      while (entry.pending) {
        const request = entry.pending;
        entry.pending = null;
        entry.inFlight = request;
        try {
          const response = await send(entry.id, request.rating);
          const serverRating = ratingValueFromPayload(response, request.rating);
          entry.serverRating = serverRating;
          notify({
            id: entry.id,
            requestedRating: request.rating,
            serverRating,
            response,
            error: null,
            latest: entry.pending === null && entry.sequence === request.sequence,
          });
        } catch (error) {
          notify({
            id: entry.id,
            requestedRating: request.rating,
            serverRating: entry.serverRating,
            response: null,
            error,
            latest: entry.pending === null && entry.sequence === request.sequence,
          });
        } finally {
          entry.inFlight = null;
        }
      }
    }

    return {
      enqueue(id, rating) {
        const entry = entryFor(id);
        entry.sequence += 1;
        entry.pending = { rating: normaliseRating(rating), sequence: entry.sequence };
        if (!entry.inFlight) {
          void drain(entry);
        }
        return entry.sequence;
      },
      seed(id, rating) {
        const entry = entryFor(id);
        if (entry.sequence === 0 && !entry.inFlight && !entry.pending) {
          entry.serverRating = normaliseRating(rating);
        }
      },
    };
  }

  function mediaTypeOf(item = {}) {
    return item && item.media_type === 'video' ? 'video' : 'photo';
  }

  function hasMediaTypeContract(items) {
    return Array.isArray(items)
      && items.length > 0
      && items.every((item) => item && MEDIA_TYPES.has(item.media_type));
  }

  function matchesMediaTypeFilter(items, requested) {
    if (!requested) {
      return true;
    }
    return hasMediaTypeContract(items)
      && items.every((item) => item.media_type === requested);
  }

  function similarityGroupValue(item = {}, mode = '') {
    const selectedMode = normaliseSimilarity(mode);
    if (!selectedMode || mediaTypeOf(item) === 'video') {
      return null;
    }
    const group = typeof item?.similarity_group === 'string' ? item.similarity_group : '';
    const prefix = `${selectedMode}:`;
    const size = Number(item?.similarity_size);
    if (!group.startsWith(prefix) || group.length <= prefix.length) {
      return null;
    }
    if (!Number.isInteger(size) || size < 2) {
      return null;
    }
    return group;
  }

  function similaritySectionKey(item = {}, mode = '') {
    return similarityGroupValue(item, mode) || 'ungrouped';
  }

  function similarityGroupBoundary(previous, current, mode = '') {
    if (!normaliseSimilarity(mode) || !current) {
      return false;
    }
    if (!previous) {
      return true;
    }
    return similaritySectionKey(previous, mode) !== similaritySectionKey(current, mode);
  }

  function similarityGroupTitle(item = {}, mode = '') {
    const selectedMode = normaliseSimilarity(mode);
    const label = SIMILARITY_LABELS[selectedMode] || 'Médias similaires';
    const rawSize = Number(item?.similarity_size);
    const size = Number.isInteger(rawSize) && rawSize >= 0 ? rawSize : 0;
    return `${label} · ${size} ${size === 1 ? 'image' : 'images'}`;
  }

  function buildPhotoQuery(filters = {}) {
    const pairs = [];
    for (const [source, target] of QUERY_FIELDS) {
      const value = source === 'mediaType'
        ? (filters.mediaType ?? filters.media_type)
        : source === 'deletionStatus'
          ? (filters.deletionStatus ?? filters.deletion_status)
          : filters[source];
      if (target === 'imported_only') {
        if (value === true) {
          pairs.push('imported_only=true');
        }
        continue;
      }
      if (target === 'rating') {
        if (isRatingFilterValue(value)) {
          pairs.push(`rating=${encodeURIComponent(String(normaliseRating(value)))}`);
        }
        continue;
      }
      if (target === 'deletion_status') {
        if (value === 'scheduled') {
          pairs.push('deletion_status=scheduled');
        }
        continue;
      }
      if (!isPresent(value) || (typeof value === 'string' && value.trim() === '')) {
        continue;
      }
      if (target === 'media_type' && !MEDIA_TYPES.has(String(value).trim())) {
        continue;
      }
      pairs.push(`${target}=${encodeURIComponent(String(value).trim())}`);
    }

    const similarity = normaliseSimilarity(filters.similarity);
    if (similarity) {
      pairs.push(`similarity=${encodeURIComponent(similarity)}`);
      const group = typeof filters.similarityGroup === 'string' ? filters.similarityGroup : '';
      if (new RegExp(`^${similarity}:[1-9][0-9]*$`).test(group)) {
        pairs.push(`similarity_group=${encodeURIComponent(group)}`);
      } else if (filters.similarityView === 'stacks') {
        pairs.push('similarity_view=stacks');
      }
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

  function buildZeroCountQuery() {
    return 'rating=0&limit=1';
  }

  function normaliseDeletionStatus(value) {
    return value === 'scheduled' ? 'scheduled' : '';
  }

  function zeroCountFromPayload(payload = {}) {
    const raw = payload?.total;
    if (raw === null || raw === undefined || (typeof raw === 'string' && raw.trim() === '')) {
      return null;
    }
    const count = Number(raw);
    return Number.isInteger(count) && count >= 0 ? count : null;
  }

  function deletionStatusCountFromPayload(payload = {}) {
    const raw = payload?.scheduled;
    if (raw === null || raw === undefined || (typeof raw === 'string' && raw.trim() === '')) {
      return null;
    }
    const count = Number(raw);
    return Number.isInteger(count) && count >= 0 ? count : null;
  }

  function deletionDueAt(photo = {}) {
    if (isPresent(photo.delete_after)) {
      return photo.delete_after;
    }
    if (isPresent(photo.deletion_scheduled_at)) {
      return photo.deletion_scheduled_at;
    }
    return null;
  }

  function isDeletionScheduled(photo = {}) {
    return normaliseDeletionStatus(photo.deletion_status) === 'scheduled'
      || deletionDueAt(photo) !== null;
  }

  function confirmationMessageForZeroSchedule(count) {
    const safeCount = Number.isInteger(Number(count)) && Number(count) >= 0 ? Number(count) : 0;
    return `Tous les ${safeCount} médias à 0 étoile, y compris hors des filtres actuels, seront placés en corbeille dans un mois. Remettre une étoile annule la programmation. Aucun fichier supprimé définitivement.`;
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

  function formatDuration(value) {
    if (value === null || value === undefined || value === '') {
      return '';
    }
    const seconds = Number(value);
    if (!Number.isFinite(seconds) || seconds < 0) {
      return '';
    }
    const wholeSeconds = Math.floor(seconds);
    const hours = Math.floor(wholeSeconds / 3600);
    const minutes = Math.floor((wholeSeconds % 3600) / 60);
    const remainder = wholeSeconds % 60;
    const paddedSeconds = String(remainder).padStart(2, '0');
    if (hours > 0) {
      return `${hours}:${String(minutes).padStart(2, '0')}:${paddedSeconds}`;
    }
    return `${minutes}:${paddedSeconds}`;
  }

  function formatUploadSelection(count) {
    const number = Number(count);
    if (!Number.isFinite(number) || number <= 0) {
      return '';
    }
    return `${number} fichier${number === 1 ? '' : 's'} prêt${number === 1 ? '' : 's'} à importer`;
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

    if (mediaTypeOf(merged) === 'video') {
      add('Prise de vue', firstExifValue([
        merged.date,
        exif.DateTimeOriginal,
        exif.CreateDate,
        exif.ModifyDate,
      ]), formatCaptureDate);
      add('Appareil', firstExifValue([merged.camera, exif.Model, exif.Make]));
      const width = firstExifValue([merged.width, exif.ImageWidth, exif.ExifImageWidth]);
      const height = firstExifValue([merged.height, exif.ImageHeight, exif.ExifImageHeight]);
      if (isPresent(width) && isPresent(height)) {
        add('Dimensions', `${width} × ${height} px`);
      }
      add('Durée', firstExifValue([merged.duration, exif.Duration, exif.DurationSeconds]), formatDuration);
      add('Taille', firstExifValue([merged.size, exif.FileSize]), formatBytes);
      add('Format', firstExifValue([merged.format, exif.FileType, exif.FileTypeExtension]));
      return rows;
    }

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

  function formatDeletionDueDate(value) {
    const formatted = formatDate(value, { short: true });
    return formatted === '—' ? '' : formatted;
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

  const RAW_FORMATS = new Set([
    'ARW',
    'CR2',
    'CR3',
    'DNG',
    'ERF',
    'NEF',
    'ORF',
    'RAF',
    'RAW',
    'RW2',
    'SRW',
    'X3F',
  ]);

  function cleanShareFilename(value) {
    if (typeof value !== 'string') {
      return '';
    }
    const unquoted = value.trim().replace(/^['"]|['"]$/g, '');
    if (!unquoted) {
      return '';
    }
    try {
      return decodeURIComponent(unquoted);
    } catch {
      return unquoted;
    }
  }

  function shareFileName(photo = {}, contentDisposition = '', sourceUrl = '') {
    const disposition = typeof contentDisposition === 'string' ? contentDisposition : '';
    const encodedMatch = disposition.match(/filename\*\s*=\s*(?:UTF-8''|[^']*'[^']*')?([^;]+)/i);
    const plainMatch = disposition.match(/filename\s*=\s*([^;]+)/i);
    const headerName = cleanShareFilename(encodedMatch?.[1] || plainMatch?.[1]);
    if (headerName) {
      return headerName;
    }

    const itemName = cleanShareFilename(photo?.name);
    if (itemName) {
      return itemName.split(/[\\/]/).pop() || itemName;
    }

    const urlName = cleanShareFilename(String(sourceUrl || '').split(/[?#]/, 1)[0].split('/').pop());
    return urlName || 'media';
  }

  function shareFileType(blobType = '', headerType = '') {
    const normaliseMime = (value) => {
      const mime = String(value || '').split(';', 1)[0].trim().toLowerCase();
      return /^[a-z0-9!#$&^_.+*-]+\/[a-z0-9!#$&^_.+*-]+$/.test(mime) ? mime : '';
    };
    return normaliseMime(blobType) || normaliseMime(headerType) || 'application/octet-stream';
  }

  function isRawMedia(photo = {}, fileName = '') {
    const format = String(photo?.format || '').trim().toUpperCase();
    if (RAW_FORMATS.has(format)) {
      return true;
    }
    const name = String(fileName || photo?.name || '');
    const extension = name.match(/\.([a-z0-9]+)$/i)?.[1]?.toUpperCase() || '';
    return RAW_FORMATS.has(extension);
  }

  function clampViewerZoom(value, min = 1, max = 5) {
    const number = Number(value);
    if (!Number.isFinite(number)) {
      return min;
    }
    return Math.min(max, Math.max(min, number));
  }

  function viewerRectEdges(rect = {}) {
    const left = Number(rect.left) || 0;
    const top = Number(rect.top) || 0;
    const width = Math.max(0, Number(rect.width) || 0);
    const height = Math.max(0, Number(rect.height) || 0);
    const right = Number.isFinite(Number(rect.right)) ? Number(rect.right) : left + width;
    const bottom = Number.isFinite(Number(rect.bottom)) ? Number(rect.bottom) : top + height;
    return { left, top, right, bottom, width: right - left, height: bottom - top };
  }

  function clampAxis(value, minimum, maximum) {
    return Math.min(maximum, Math.max(minimum, Number(value) || 0));
  }

  function viewerPanBounds({baseRect, viewportRect, zoom}) {
    const base = viewerRectEdges(baseRect);
    const viewport = viewerRectEdges(viewportRect);
    const safeZoom = clampViewerZoom(zoom);
    const centerX = (base.left + base.right) / 2;
    const centerY = (base.top + base.bottom) / 2;
    const scaledWidth = base.width * safeZoom;
    const scaledHeight = base.height * safeZoom;
    const scaledLeft = centerX - scaledWidth / 2;
    const scaledRight = centerX + scaledWidth / 2;
    const scaledTop = centerY - scaledHeight / 2;
    const scaledBottom = centerY + scaledHeight / 2;
    return {
      x: scaledWidth > viewport.width
        ? { min: viewport.right - scaledRight, max: viewport.left - scaledLeft }
        : { min: 0, max: 0 },
      y: scaledHeight > viewport.height
        ? { min: viewport.bottom - scaledBottom, max: viewport.top - scaledTop }
        : { min: 0, max: 0 },
    };
  }

  function clampViewerPan(pan = {}, {baseRect, viewportRect, zoom} = {}) {
    const bounds = viewerPanBounds({baseRect, viewportRect, zoom});
    return {
      x: clampAxis(pan.x, bounds.x.min, bounds.x.max),
      y: clampAxis(pan.y, bounds.y.min, bounds.y.max),
    };
  }

  function zoomViewerAroundPoint({
    baseRect,
    viewportRect,
    currentZoom,
    currentPan = {x: 0, y: 0},
    nextZoom,
    point,
  }) {
    const base = viewerRectEdges(baseRect);
    const center = {
      x: (base.left + base.right) / 2,
      y: (base.top + base.bottom) / 2,
    };
    const safeCurrentZoom = clampViewerZoom(currentZoom);
    const safeNextZoom = clampViewerZoom(nextZoom);
    const sourcePoint = {
      x: center.x + (Number(point?.x) - center.x - (Number(currentPan.x) || 0)) / safeCurrentZoom,
      y: center.y + (Number(point?.y) - center.y - (Number(currentPan.y) || 0)) / safeCurrentZoom,
    };
    const rawPan = {
      x: Number(point?.x) - center.x - safeNextZoom * (sourcePoint.x - center.x),
      y: Number(point?.y) - center.y - safeNextZoom * (sourcePoint.y - center.y),
    };
    return {
      zoom: safeNextZoom,
      pan: clampViewerPan(rawPan, {baseRect, viewportRect, zoom: safeNextZoom}),
    };
  }

  function pinchViewerTransform({
    baseRect,
    viewportRect,
    startZoom,
    startPan = {x: 0, y: 0},
    startDistance,
    currentDistance,
    startMidpoint,
    currentMidpoint,
  }) {
    const safeStartDistance = Math.max(0.0001, Number(startDistance) || 0);
    const safeStartZoom = clampViewerZoom(startZoom);
    const nextZoom = clampViewerZoom(
      safeStartZoom * ((Number(currentDistance) || safeStartDistance) / safeStartDistance),
    );
    const base = viewerRectEdges(baseRect);
    const center = {
      x: (base.left + base.right) / 2,
      y: (base.top + base.bottom) / 2,
    };
    const sourcePoint = {
      x: center.x + (Number(startMidpoint?.x) - center.x - (Number(startPan.x) || 0)) / safeStartZoom,
      y: center.y + (Number(startMidpoint?.y) - center.y - (Number(startPan.y) || 0)) / safeStartZoom,
    };
    const rawPan = {
      x: Number(currentMidpoint?.x) - center.x - nextZoom * (sourcePoint.x - center.x),
      y: Number(currentMidpoint?.y) - center.y - nextZoom * (sourcePoint.y - center.y),
    };
    return {
      zoom: nextZoom,
      pan: clampViewerPan(rawPan, {baseRect, viewportRect, zoom: nextZoom}),
    };
  }

  const helpers = {
    buildPhotoQuery,
    buildZeroCountQuery,
    confirmationMessageForZeroSchedule,
    deletionDueAt,
    isDeletionScheduled,
    normaliseRating,
    normaliseDeletionStatus,
    deletionStatusCountFromPayload,
    createRatingQueue,
    formatBytes,
    formatDuration,
    formatUploadSelection,
    getPhotoAspectRatio,
    formatExifValue,
    zeroCountFromPayload,
    hasMediaTypeContract,
    matchesMediaTypeFilter,
    mediaTypeOf,
    formatDate,
    formatDeletionDueDate,
    sessionPresentation,
    buildExifRows,
    normaliseSimilarity,
    similaritySectionKey,
    similarityGroupBoundary,
    similarityGroupTitle,
    shareFileName,
    shareFileType,
    isRawMedia,
    clampViewerZoom,
    clampViewerPan,
    zoomViewerAroundPoint,
    pinchViewerTransform,
  };

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = helpers;
  }

  if (typeof document === 'undefined') {
    return;
  }

  const PAGE_SIZE = 24;
  const POLL_INTERVAL = 1800;
  const SIMILARITY_POLL_INTERVAL = 1500;
  const state = {
    authenticated: false,
    sessionLabel: 'connecté',
    items: [],
    entries: [],
    mediaTotal: null,
    activeGroup: null,
    stackReturn: null,
    mediaContract: null,
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
    similarityStatusRequest: null,
    similarityStatusSequence: 0,
    similarityStatusTimer: null,
    similarityRunning: null,
    similarityStatus: null,
    similarityCancelRequested: false,
    deletionSummaryRequest: null,
    deletionSummarySequence: 0,
    deletionSummaryTimer: null,
    zeroDeletionCount: null,
    scheduledDeletionCount: null,
    trashedDeletionCount: null,
    deletionDueAt: null,
    deletionScheduleInFlight: false,
    deletionPreviousFocus: null,
    deletionDialogCount: null,
    refreshAfterScan: false,
    scanPending: false,
    searchTimer: null,
    selectedFiles: [],
    uploadInFlight: false,
    uploadPending: null,
    uploadErrorCount: 0,
    lightboxIndex: -1,
    lightboxRequest: null,
    lightboxSequence: 0,
    shareSequence: 0,
    sharePhotoId: null,
    shareController: null,
    sharePreviewController: null,
    sharePromise: null,
    sharePreviewPromise: null,
    shareFiles: new Map(),
    sharePreviewFiles: new Map(),
    shareInFlight: false,
    previousFocus: null,
    swipeStartX: null,
    swipeStartY: null,
    swipePointerId: null,
    viewerZoom: 1,
    viewerPanX: 0,
    viewerPanY: 0,
    viewerBaseRect: null,
    viewerPointers: new Map(),
    viewerGesture: null,
    viewerGestureHadMultiple: false,
    viewerLastTap: null,
    galleryLayoutFrame: null,
    galleryLayoutWidth: null,
    ratingQueue: null,
    ratingFilterRefreshPending: false,
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
    scanButtonLabel: document.getElementById('scan-button-label'),
    uploadForm: document.getElementById('upload-form'),
    uploadInput: document.getElementById('upload-input'),
    uploadToggle: document.getElementById('upload-toggle'),
    uploadPanel: document.getElementById('upload-panel'),
    uploadDropzone: document.getElementById('upload-dropzone'),
    uploadChoose: document.getElementById('upload-choose'),
    uploadSubmit: document.getElementById('upload-submit'),
    uploadSummary: document.getElementById('upload-summary'),
    uploadSelection: document.getElementById('upload-selection'),
    uploadErrors: document.getElementById('upload-errors'),
    searchInput: document.getElementById('search-input'),
    sortSelect: document.getElementById('sort-select'),
    mediaTypeSelect: document.getElementById('media-type-filter'),
    importedOnlyFilter: document.getElementById('imported-only-filter'),
    ratingFilter: document.getElementById('rating-filter'),
    deletionStatusFilter: document.getElementById('deletion-status-filter'),
    similaritySelect: document.getElementById('similarity-select'),
    similarityAnalyze: document.getElementById('similarity-analyze'),
    similarityCancel: document.getElementById('similarity-cancel'),
    similarityStatus: document.getElementById('similarity-status'),
    similarityStatusText: document.getElementById('similarity-status-text'),
    gridSize: document.getElementById('grid-size'),
    gridSizeValue: document.getElementById('grid-size-value'),
    filtersForm: document.getElementById('filters-form'),
    filterDetails: document.getElementById('filter-details'),
    filterCount: document.getElementById('filter-count'),
    cameraSelect: document.getElementById('camera-filter'),
    directorySelect: document.getElementById('directory-filter'),
    lensSelect: document.getElementById('lens-filter'),
    formatSelect: document.getElementById('format-filter'),
    dateFrom: document.getElementById('date-from'),
    dateTo: document.getElementById('date-to'),
    isoMin: document.getElementById('iso-min'),
    isoMax: document.getElementById('iso-max'),
    resultsMeta: document.getElementById('results-meta'),
    resultCount: document.getElementById('result-count'),
    groupNavigation: document.getElementById('group-navigation'),
    groupBack: document.getElementById('group-back'),
    groupTitle: document.getElementById('group-title'),
    gallery: document.getElementById('gallery-grid'),
    galleryLoading: document.getElementById('gallery-loading'),
    galleryEmpty: document.getElementById('gallery-empty'),
    galleryError: document.getElementById('gallery-error'),
    galleryErrorText: document.getElementById('gallery-error-text'),
    loadMore: document.getElementById('load-more'),
    scheduleZeroButton: document.getElementById('schedule-zero-button'),
    zeroDeletionCount: document.getElementById('zero-deletion-count'),
    deletionSummary: document.getElementById('deletion-summary'),
    lightbox: document.getElementById('lightbox'),
    lightboxTitle: document.getElementById('lightbox-title'),
    lightboxPosition: document.getElementById('lightbox-position'),
    lightboxImage: document.getElementById('lightbox-image'),
    lightboxVideo: document.getElementById('lightbox-video'),
    lightboxImageLoader: document.getElementById('lightbox-image-loader'),
    lightboxImageFrame: document.getElementById('lightbox-image-frame'),
    lightboxMedia: document.querySelector('.lightbox-media'),
    lightboxResetZoom: document.getElementById('lightbox-reset-zoom'),
    lightboxRating: document.getElementById('lightbox-rating'),
    lightboxShare: document.getElementById('lightbox-share'),
    lightboxSharePreview: document.getElementById('lightbox-share-preview'),
    lightboxExif: document.getElementById('lightbox-exif-list'),
    lightboxLoading: document.getElementById('lightbox-loading'),
    lightboxError: document.getElementById('lightbox-error'),
    lightboxDeletionStatus: document.getElementById('lightbox-deletion-status'),
    lightboxShareStatus: document.getElementById('lightbox-share-status'),
    lightboxPrevious: document.getElementById('lightbox-previous'),
    lightboxNext: document.getElementById('lightbox-next'),
    lightboxClose: document.getElementById('lightbox-close'),
    scheduleZeroDialog: document.getElementById('schedule-zero-dialog'),
    scheduleZeroMessage: document.getElementById('schedule-zero-message'),
    scheduleZeroCancel: document.getElementById('schedule-zero-cancel'),
    scheduleZeroConfirm: document.getElementById('schedule-zero-confirm'),
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
    } else if (element === dom.similarityStatus && dom.similarityStatusText) {
      dom.similarityStatusText.textContent = message || '';
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
    [
      state.photosRequest,
      state.facetsRequest,
      state.statusRequest,
      state.similarityStatusRequest,
      state.deletionSummaryRequest,
      state.lightboxRequest,
    ]
      .filter(Boolean)
      .forEach((controller) => controller.abort());
    stopSimilarityPolling();
    if (state.deletionSummaryTimer !== null) {
      window.clearTimeout(state.deletionSummaryTimer);
      state.deletionSummaryTimer = null;
    }
    state.photosSequence += 1;
    state.facetsSequence += 1;
    state.statusSequence += 1;
    state.similarityStatusSequence += 1;
    state.deletionSummarySequence += 1;
    state.lightboxSequence += 1;
  }

  function showLogin(message = '') {
    clearGroupContext();
    cancelPendingRequests();
    state.authenticated = false;
    state.sessionLabel = 'connecté';
    stopStatusPolling();
    state.similarityRunning = null;
    state.similarityStatus = null;
    state.similarityCancelRequested = false;
    if (dom.lightbox.open || dom.lightbox.hasAttribute('open')) {
      closeLightbox();
    }
    closeScheduleZeroDialog();
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
    void refreshSimilarityStatus();
    void refreshDeletionSummary();
  }

  function currentFilters() {
    return {
      q: dom.searchInput?.value || '',
      directory: dom.directorySelect.value,
      camera: dom.cameraSelect.value,
      lens: dom.lensSelect.value,
      format: dom.formatSelect.value,
      dateFrom: dom.dateFrom.value,
      dateTo: dom.dateTo.value,
      isoMin: dom.isoMin.value,
      isoMax: dom.isoMax.value,
      sort: dom.sortSelect.value,
      mediaType: dom.mediaTypeSelect.value,
      importedOnly: dom.importedOnlyFilter.checked,
      rating: dom.ratingFilter.value,
      deletionStatus: dom.deletionStatusFilter.value,
      similarity: dom.similaritySelect?.value || '',
      similarityView: state.activeGroup ? '' : 'stacks',
      similarityGroup: state.activeGroup,
      offset: state.offset,
      limit: state.activeGroup ? 200 : PAGE_SIZE,
    };
  }

  function photoRating(photo = {}) {
    return normaliseRating(photo?.rating);
  }

  function withNormalisedRating(photo = {}) {
    return { ...photo, rating: photoRating(photo) };
  }

  function clearFiltersForImportedOnly() {
    if (dom.searchInput) {
      dom.searchInput.value = '';
    }
    dom.mediaTypeSelect.value = '';
    dom.ratingFilter.value = '';
    dom.deletionStatusFilter.value = '';
    if (dom.similaritySelect) {
      dom.similaritySelect.value = '';
    }
    dom.directorySelect.value = '';
    dom.cameraSelect.value = '';
    dom.lensSelect.value = '';
    dom.formatSelect.value = '';
    dom.dateFrom.value = '';
    dom.dateTo.value = '';
    dom.isoMin.value = '';
    dom.isoMax.value = '';
  }

  function selectImportedOnly() {
    clearGroupContext();
    clearFiltersForImportedOnly();
    dom.importedOnlyFilter.checked = true;
    updateFilterCount();
  }

  function applyUrlFilters() {
    const params = new URLSearchParams(window.location.search);
    if (params.get('imported_only') === 'true') {
      selectImportedOnly();
      return;
    }
    if (isRatingFilterValue(params.get('rating'))) {
      dom.ratingFilter.value = String(normaliseRating(params.get('rating')));
    }
    if (normaliseDeletionStatus(params.get('deletion_status'))) {
      dom.deletionStatusFilter.value = 'scheduled';
    }
    const similarity = normaliseSimilarity(params.get('similarity'));
    if (similarity && dom.similaritySelect) {
      dom.similaritySelect.value = similarity;
    }
  }

  function updateFilterCount() {
    const values = [
      dom.mediaTypeSelect.value,
      dom.importedOnlyFilter.checked ? 'true' : '',
      dom.ratingFilter.value,
      dom.deletionStatusFilter.value,
      dom.similaritySelect?.value || '',
      dom.directorySelect.value,
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
    dom.gallery.closest('.gallery-section')?.classList.toggle('gallery-section--stacks', isStackOverview());
    const loadedCount = state.entries.length;
    if (state.total === null) {
      dom.resultCount.textContent = loadedCount ? formatCount(loadedCount, 'média', 'médias') : '—';
    } else {
      dom.resultCount.textContent = isStackOverview()
        ? `${loadedCount} sur ${state.total} éléments · ${state.mediaTotal ?? '—'} médias`
        : `${loadedCount} sur ${state.total} ${state.total === 1 ? 'média' : 'médias'}`;
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
    const duration = formatDuration(photo.duration);
    if (format !== '—') {
      metadata.append(textElement('span', 'metadata-label', format));
    }
    if (date !== '—') {
      metadata.append(textElement('span', '', date));
    }
    if (mediaTypeOf(photo) === 'video' && duration) {
      metadata.append(textElement('span', 'photo-card__duration', duration));
    }
    parent.append(metadata);
  }

  function deletionBadgeText(photo = {}) {
    const due = formatDeletionDueDate(deletionDueAt(photo));
    return due ? `Corbeille · ${due}` : 'Corbeille programmée';
  }

  function makeDeletionBadge(photo = {}) {
    if (!isDeletionScheduled(photo)) {
      return null;
    }
    const badge = textElement('span', 'photo-card__deletion');
    badge.append(
      textElement('span', 'photo-card__deletion-icon', '◷'),
      textElement('span', '', deletionBadgeText(photo)),
    );
    badge.setAttribute('aria-label', deletionBadgeText(photo));
    return badge;
  }

  function renderCardDeletionBadge(card, photo) {
    const frame = card?.querySelector('.photo-card__frame');
    if (!frame) {
      return;
    }
    frame.querySelector('.photo-card__deletion')?.remove();
    const badge = makeDeletionBadge(photo);
    if (badge) {
      frame.append(badge);
    }
  }

  function updateDeletionUiForId(id) {
    const photo = findPhotoById(id);
    if (!photo) {
      return;
    }
    [...dom.gallery.querySelectorAll('.photo-card')]
      .filter((card) => card.dataset.photoId === String(id))
      .forEach((card) => renderCardDeletionBadge(card, photo));
    if (state.lightboxIndex >= 0 && String(state.items[state.lightboxIndex]?.id) === String(id)) {
      renderLightboxDeletionStatus(photo);
    }
  }

  function renderLightboxDeletionStatus(photo = {}) {
    if (!dom.lightboxDeletionStatus) {
      return;
    }
    if (!isDeletionScheduled(photo)) {
      setMessage(dom.lightboxDeletionStatus, '');
      return;
    }
    const due = formatDeletionDueDate(deletionDueAt(photo));
    setMessage(
      dom.lightboxDeletionStatus,
      due ? `Corbeille prévue le ${due}.` : 'Corbeille programmée.',
      'info',
    );
  }

  function ratingDescription(rating) {
    return rating === 1 ? '1 étoile' : `${rating} étoiles`;
  }

  function setRatingControlState(control, rating, { pending = false } = {}) {
    if (!control) {
      return;
    }
    const value = normaliseRating(rating);
    control.dataset.rating = String(value);
    control.setAttribute(
      'aria-label',
      `Note : ${ratingDescription(value)} sur 4. Suppr. ou Retour arrière pour effacer.`,
    );
    control.setAttribute('aria-keyshortcuts', 'Delete Backspace');
    control.classList.toggle('is-pending', pending);
    if (pending) {
      control.setAttribute('aria-busy', 'true');
    } else {
      control.removeAttribute('aria-busy');
    }
    control.querySelectorAll('button[data-rating-value]').forEach((button) => {
      const starValue = Number(button.dataset.ratingValue);
      const selected = starValue <= value;
      button.classList.toggle('is-filled', selected);
      const glyph = button.querySelector('span[aria-hidden="true"]');
      if (glyph) {
        glyph.textContent = selected ? '★' : '☆';
      }
      button.setAttribute('aria-pressed', String(selected));
      button.setAttribute(
        'aria-label',
        starValue === value && value > 0
          ? `Effacer la note de ${ratingDescription(value)}`
          : `Noter ${ratingDescription(starValue)}`,
      );
    });
  }

  function setRatingControlError(control, message = '') {
    const error = control?.querySelector('.rating-control__error');
    if (!error) {
      return;
    }
    error.textContent = message;
    setHidden(error, !message);
  }

  function ratingControlsForId(id) {
    return [...document.querySelectorAll('.rating-control')]
      .filter((control) => control.dataset.photoId === String(id));
  }

  function findPhotoById(id) {
    return state.items.find((item) => String(item.id) === String(id)) || null;
  }

  function ratingPayloadItem(payload) {
    if (payload && typeof payload === 'object' && payload.item && typeof payload.item === 'object') {
      return payload.item;
    }
    return payload && typeof payload === 'object' ? payload : null;
  }

  function mergeRatingResponse(id, payload, rating) {
    const index = state.items.findIndex((item) => String(item.id) === String(id));
    if (index < 0) {
      return;
    }
    const current = state.items[index];
    const incoming = ratingPayloadItem(payload);
    const matchingIncoming = incoming && incoming.id !== undefined
      && String(incoming.id) === String(id)
      ? incoming
      : null;
    state.items[index] = {
      ...current,
      ...(matchingIncoming || {}),
      rating: normaliseRating(rating),
    };
    if (normaliseRating(rating) >= 1) {
      state.items[index].deletion_scheduled_at = matchingIncoming
        && Object.prototype.hasOwnProperty.call(matchingIncoming, 'deletion_scheduled_at')
        ? matchingIncoming.deletion_scheduled_at
        : null;
      state.items[index].delete_after = matchingIncoming
        && Object.prototype.hasOwnProperty.call(matchingIncoming, 'delete_after')
        ? matchingIncoming.delete_after
        : null;
      if (state.items[index].deletion_scheduled_at === null && state.items[index].delete_after === null) {
        delete state.items[index].deletion_status;
      }
    }
  }

  function applyRatingToUi(id, rating, { pending = false, error = '' } = {}) {
    const value = normaliseRating(rating);
    const photo = findPhotoById(id);
    if (photo) {
      photo.rating = value;
    }
    ratingControlsForId(id).forEach((control) => {
      setRatingControlState(control, value, { pending });
      setRatingControlError(control, error);
    });
  }

  function isLightboxOpen() {
    return Boolean(dom.lightbox?.open || dom.lightbox?.hasAttribute('open'));
  }

  function hasActiveRatingFilter() {
    return isRatingFilterValue(dom.ratingFilter?.value);
  }

  function hasActiveDeletionFilter() {
    return normaliseDeletionStatus(dom.deletionStatusFilter?.value) === 'scheduled';
  }

  function hasActiveMutationFilter() {
    return hasActiveRatingFilter() || hasActiveDeletionFilter();
  }

  function persistRating(id, rating) {
    return fetchJson(`/api/photos/${encodeURIComponent(String(id))}/rating`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ rating: normaliseRating(rating) }),
    });
  }

  function handleRatingMutationSettled(event) {
    const id = String(event.id);
    const optimisticRating = photoRating(findPhotoById(id) || {});
    const effectiveRating = event.latest ? event.serverRating : optimisticRating;
    mergeRatingResponse(id, event.response, effectiveRating);

    if (!event.latest) {
      ratingControlsForId(id).forEach((control) => {
        setRatingControlState(control, effectiveRating, { pending: true });
      });
      return;
    }

    if (event.error) {
      const message = `La note n’a pas pu être enregistrée. ${displayErrorMessage(event.error, 'Réessayez.')}`;
      applyRatingToUi(id, event.serverRating, { error: message });
      updateDeletionUiForId(id);
      scheduleDeletionSummaryRefresh();
      return;
    }

    applyRatingToUi(id, event.serverRating);
    updateDeletionUiForId(id);
    scheduleDeletionSummaryRefresh();
    if (hasActiveMutationFilter()) {
      if (isLightboxOpen()) {
        state.ratingFilterRefreshPending = true;
      } else {
        void refreshPhotosPreservingScroll();
      }
    }
  }

  function ensureRatingQueue() {
    if (!state.ratingQueue) {
      state.ratingQueue = createRatingQueue(persistRating, { onSettled: handleRatingMutationSettled });
    }
    return state.ratingQueue;
  }

  function requestPhotoRating(id, rating) {
    const key = String(id);
    const photo = findPhotoById(key);
    if (!photo) {
      return;
    }
    const value = normaliseRating(rating);
    const queue = ensureRatingQueue();
    queue.seed(key, photoRating(photo));
    setRatingControlErrorForId(key, '');
    applyRatingToUi(key, value, { pending: true });
    queue.enqueue(key, value);
  }

  function setRatingControlErrorForId(id, message = '') {
    ratingControlsForId(id).forEach((control) => setRatingControlError(control, message));
  }

  function makeRatingControl(photo, placement) {
    const id = photo.id === undefined || photo.id === null ? '' : String(photo.id);
    const control = textElement('div', `rating-control rating-control--${placement}`);
    control.dataset.photoId = id;
    control.dataset.ratingTarget = placement;
    control.setAttribute('role', 'group');
    control.tabIndex = 0;
    control.addEventListener('keydown', (event) => {
      if (event.key !== 'Delete' && event.key !== 'Backspace') {
        return;
      }
      event.preventDefault();
      event.stopPropagation();
      requestPhotoRating(id, 0);
    });
    for (let value = 1; value <= 4; value += 1) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'rating-control__star';
      button.dataset.ratingValue = String(value);
      button.setAttribute('aria-pressed', 'false');
      button.innerHTML = '<span aria-hidden="true">☆</span>';
      button.addEventListener('click', (event) => {
        event.stopPropagation();
        const current = photoRating(findPhotoById(id) || photo);
        requestPhotoRating(id, value === current ? 0 : value);
      });
      control.append(button);
    }
    control.append(textElement('span', 'rating-control__error', ''));
    setRatingControlState(control, photoRating(photo));
    setRatingControlError(control, '');
    return control;
  }

  function makePhotoCard(photo, index) {
    photo = withNormalisedRating(photo);
    const mediaType = mediaTypeOf(photo);
    const isVideo = mediaType === 'video';
    const card = textElement('article', `photo-card${isVideo ? ' photo-card--video' : ''}`);
    card.dataset.photoId = photo.id === undefined || photo.id === null ? '' : String(photo.id);
    card.dataset.mediaType = mediaType;

    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'photo-card__open';
    button.setAttribute('aria-label', `Ouvrir ${photo.name || 'le média'}`);
    button.addEventListener('click', () => openLightbox(index));

    const frame = textElement('span', 'photo-card__frame');
    const assetUrl = normaliseAssetUrl(photo.thumb_url || photo.preview_url);
    const width = finiteNumber(photo.width);
    const height = finiteNumber(photo.height);
    frame.style.aspectRatio = `${getPhotoAspectRatio(photo)}`;
    if (assetUrl) {
      const image = document.createElement('img');
      image.className = 'photo-card__image';
      image.alt = photo.name || (isVideo ? 'Vidéo' : 'Photo');
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

    if (isVideo) {
      frame.append(textElement('span', 'photo-card__play', '▶'));
    }

    const overlay = textElement('span', 'photo-card__overlay');
    overlay.append(textElement('span', 'photo-card__index', String(index + 1).padStart(2, '0')));
    overlay.append(textElement('span', 'photo-card__open-hint', 'Voir'));
    frame.append(overlay);
    const deletionBadge = makeDeletionBadge(photo);
    if (deletionBadge) {
      frame.append(deletionBadge);
    }
    button.append(frame);
    const media = textElement('div', 'photo-card__media');
    media.append(button, makeRatingControl(photo, 'card'));
    card.append(media);

    const footer = textElement('div', 'photo-card__footer');
    const caption = textElement('div', 'photo-card__caption');
    caption.append(textElement('h3', 'photo-card__name', formatExifValue(photo.name)));
    appendPhotoMetadata(caption, photo);
    footer.append(caption);
    card.append(footer);
    return card;
  }

  function makeSimilarityGroupHeading(photo, mode) {
    const group = similaritySectionKey(photo, mode);
    const heading = textElement('div', 'similarity-group-heading');
    heading.dataset.similarityGroup = group;
    heading.dataset.groupId = group;
    const title = group === 'ungrouped'
      ? 'Autres médias'
      : similarityGroupTitle(photo, mode);
    heading.append(textElement('h3', 'similarity-group-heading__title', title));
    return heading;
  }

  function isStackOverview() {
    return Boolean(normaliseSimilarity(dom.similaritySelect?.value) && !state.activeGroup);
  }

  function clearGroupContext() {
    state.activeGroup = null;
    state.stackReturn = null;
    if (dom.groupNavigation) dom.groupNavigation.hidden = true;
  }

  async function openStack(entry, trigger) {
    if (state.activeGroup || !entry.similarity_group) return;
    const fragment = document.createDocumentFragment();
    const snapshot = {
      fragment, trigger, entries: state.entries, items: state.items,
      total: state.total, mediaTotal: state.mediaTotal, offset: state.offset,
      hasMore: state.hasMore, scrollX: window.scrollX, scrollY: window.scrollY,
    };
    while (dom.gallery.firstChild) fragment.append(dom.gallery.firstChild);
    state.stackReturn = snapshot;
    state.activeGroup = entry.similarity_group;
    dom.groupTitle.textContent = similarityGroupTitle(entry, dom.similaritySelect.value);
    dom.groupNavigation.hidden = false;
    await loadPhotos({ reset: true });
    if (state.activeGroup === entry.similarity_group) {
      const top = dom.gallery.getBoundingClientRect().top + window.scrollY - dom.groupNavigation.offsetHeight - 12;
      window.scrollTo(0, Math.max(0, top));
      dom.groupTitle.focus({ preventScroll: true });
    }
  }

  function backToStacks() {
    const snapshot = state.stackReturn;
    state.photosRequest?.abort();
    state.photosRequest = null;
    state.photosSequence += 1;
    clearGroupContext();
    if (!snapshot) {
      void loadPhotos({ reset: true });
      return;
    }
    Object.assign(state, {
      entries: snapshot.entries, items: snapshot.items, total: snapshot.total,
      mediaTotal: snapshot.mediaTotal, offset: snapshot.offset, hasMore: snapshot.hasMore,
    });
    dom.gallery.replaceChildren(snapshot.fragment);
    dom.gallery.classList.remove('gallery-grid--grouped');
    updateResultsMeta();
    showGalleryState(state.entries.length ? 'grid' : 'empty');
    scheduleGalleryLayout();
    const restore = () => {
      window.scrollTo(snapshot.scrollX, snapshot.scrollY);
      snapshot.trigger?.focus({ preventScroll: true });
    };
    restore();
    window.requestAnimationFrame(restore);
  }

  function makeStackCard(entry) {
    const card = textElement('article', 'photo-card stack-card');
    card.dataset.similarityGroup = entry.similarity_group;
    card.dataset.stackSize = String(entry.similarity_size);
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'photo-card__open stack-card__open';
    button.setAttribute('aria-label', `Ouvrir le groupe de ${entry.similarity_size} images`);
    button.addEventListener('click', () => { void openStack(entry, button); });
    const frame = textElement('span', 'photo-card__frame stack-card__frame');
    frame.style.aspectRatio = String(getPhotoAspectRatio(entry));
    const covers = Array.isArray(entry.stack_covers) && entry.stack_covers.length ? entry.stack_covers : [entry];
    covers.slice(0, 3).reverse().forEach((cover, index, selected) => {
      const layer = textElement('span', 'stack-card__layer');
      const depth = selected.length - index - 1;
      layer.dataset.depth = String(depth);
      layer.style.setProperty('--stack-depth', depth);
      const image = document.createElement('img');
      image.className = 'photo-card__image';
      image.alt = depth === 0 ? (cover.name || 'Photo de couverture du groupe') : '';
      image.loading = 'lazy';
      image.decoding = 'async';
      image.addEventListener('load', scheduleGalleryLayout, { once: true });
      image.addEventListener('error', () => { layer.classList.add('is-missing'); }, { once: true });
      image.src = normaliseAssetUrl(cover.thumb_url || cover.preview_url);
      layer.append(image);
      frame.append(layer);
    });
    frame.append(textElement('span', 'stack-card__count', `${entry.similarity_size} images`));
    button.append(frame);
    const footer = textElement('div', 'photo-card__footer');
    footer.append(textElement('h3', 'photo-card__name', 'Ouvrir le groupe'));
    footer.append(textElement('span', 'stack-card__label', SIMILARITY_LABELS[normaliseSimilarity(dom.similaritySelect?.value)] || 'Médias similaires'));
    card.append(button, footer);
    return card;
  }

  function renderGallery({ reset = true } = {}) {
    const existingCount = reset ? 0 : dom.gallery.querySelectorAll('.photo-card').length;
    if (reset) dom.gallery.replaceChildren();
    dom.gallery.classList.toggle('gallery-grid--grouped', Boolean(state.activeGroup));
    state.entries.slice(existingCount).forEach((entry) => {
      const index = state.items.findIndex(photo => String(photo.id) === String(entry.id));
      dom.gallery.append(entry.is_stack ? makeStackCard(entry) : makePhotoCard(entry, index));
    });
    updateResultsMeta();
    if (state.entries.length === 0) {
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

  async function loadPhotos({ reset = true, preserveViewport = false } = {}) {
    if (state.photosRequest) {
      state.photosRequest.abort();
    }
    const controller = new AbortController();
    state.photosRequest = controller;
    const sequence = ++state.photosSequence;
    const preservedScroll = preserveViewport
      ? { left: window.scrollX, top: window.scrollY }
      : null;
    if (reset) {
      state.offset = 0;
      if (!preserveViewport) {
        state.items = [];
        state.entries = [];
        state.total = null;
        showGalleryState('loading');
        updateResultsMeta();
      }
    }

    try {
      const filters = currentFilters();
      const query = buildPhotoQuery(filters);
      const payload = await fetchJson(`/api/photos?${query}`, { signal: controller.signal });
      if (sequence !== state.photosSequence || controller.signal.aborted) {
        return;
      }
      let incoming = Array.isArray(payload?.items)
        ? payload.items.map(withNormalisedRating)
        : [];
      const reportedTotal = finiteNumber(payload?.total);
      if (state.activeGroup && reportedTotal !== null) {
        while (incoming.length < reportedTotal) {
          const moreQuery = buildPhotoQuery({ ...filters, offset: incoming.length, limit: 200 });
          const more = await fetchJson(`/api/photos?${moreQuery}`, { signal: controller.signal });
          if (sequence !== state.photosSequence || controller.signal.aborted) return;
          if (!Array.isArray(more?.items) || more.items.length === 0) {
            throw new Error('Le groupe a changé pendant son ouverture. Réessayez.');
          }
          incoming.push(...more.items.map(withNormalisedRating));
        }
      }
      const requestedMediaType = currentFilters().mediaType;
      const mediaContractAvailable = hasMediaTypeContract(incoming);
      const requestedTypeMatches = matchesMediaTypeFilter(incoming, requestedMediaType);
      if (incoming.length > 0) {
        state.mediaContract = mediaContractAvailable;
      }
      if (requestedMediaType && incoming.length > 0 && !requestedTypeMatches) {
        state.items = [];
        state.entries = [];
        state.total = null;
        state.offset = 0;
        state.hasMore = false;
        const label = requestedMediaType === 'video' ? 'Vidéos' : 'Photos';
        renderGalleryError(`Le filtre ${label} nécessite la version média du serveur.`);
        return;
      }
      const currentMedia = new Map(state.items.map(item => [String(item.id), item]));
      const previous = state.entries.map(entry => entry.is_stack ? entry : currentMedia.get(String(entry.id)) || entry);
      state.entries = reset ? incoming : previous.concat(incoming);
      state.items = state.entries.filter(item => !item.is_stack);
      state.total = reportedTotal === null ? null : reportedTotal;
      state.mediaTotal = finiteNumber(payload?.media_total);
      state.offset = state.entries.length;
      state.hasMore = !state.activeGroup && incoming.length >= PAGE_SIZE;
      renderGallery({ reset });
      if (preservedScroll) {
        window.scrollTo(preservedScroll.left, preservedScroll.top);
        window.requestAnimationFrame(() => window.scrollTo(preservedScroll.left, preservedScroll.top));
      }
    } catch (error) {
      if (controller.signal.aborted || sequence !== state.photosSequence) {
        return;
      }
      if (reset && !preserveViewport) {
        state.items = [];
        state.entries = [];
        state.total = null;
      } else if (preserveViewport) {
        setMessage(dom.scanStatus, displayErrorMessage(error, 'Les résultats n’ont pas pu être actualisés.'), 'error');
        return;
      }
      renderGalleryError(displayErrorMessage(error, 'Les médias ne peuvent pas être chargés pour le moment.'));
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

  function fillDirectorySelect(values) {
    const select = dom.directorySelect;
    const previous = select.value;
    select.replaceChildren(new Option('Tous les dossiers', ''));
    const entries = (Array.isArray(values) ? values : [])
      .filter((entry) => entry && typeof entry.value === 'string' && typeof entry.label === 'string')
      .sort((a, b) => a.label.localeCompare(b.label, 'fr', { sensitivity: 'base' }));
    for (const entry of entries) {
      select.append(new Option(`${entry.label} (${entry.count})`, entry.value));
    }
    if (entries.some((entry) => entry.value === previous)) {
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
      fillDirectorySelect(payload?.directories);
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
      let message = 'Indexation en cours';
      if (processed !== null && total !== null) {
        message += ` · ${processed} / ${total}`;
      } else if (processed !== null) {
        message += ` · ${processed} traité${processed === 1 ? '' : 's'}`;
      }
      setMessage(dom.scanStatus, message, 'info');
      dom.sessionStatus.textContent = 'indexation en cours';
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

  function similarityErrors(payload = {}) {
    return Array.isArray(payload.errors) ? payload.errors : [];
  }

  function similarityNumber(payload, key, fallback = 0) {
    const value = finiteNumber(payload?.[key]);
    return value === null || value < 0 ? fallback : value;
  }

  function similarityHasResult(payload = {}) {
    const groups = payload?.groups && typeof payload.groups === 'object' ? payload.groups : {};
    return similarityNumber(payload, 'analyzed') > 0
      || similarityNumber(payload, 'cached') > 0
      || similarityNumber(groups, 'near') > 0
      || similarityNumber(groups, 'content') > 0;
  }

  function similarityErrorSummary(errors) {
    const messages = errors.map(errorValue).filter(Boolean).slice(0, 2);
    if (!messages.length) {
      return '';
    }
    return ` · ${messages.join(' · ')}`;
  }

  function renderSimilarityStatus(payload = {}) {
    const running = Boolean(payload.running);
    const errors = similarityErrors(payload);
    const processed = similarityNumber(payload, 'processed');
    const total = finiteNumber(payload.total);
    const cached = similarityNumber(payload, 'cached');
    const analyzed = similarityNumber(payload, 'analyzed');
    const cancelled = payload.cancelled === true;
    state.similarityRunning = running;
    state.similarityStatus = payload;

    if (dom.similarityAnalyze) {
      dom.similarityAnalyze.textContent = similarityHasResult(payload)
        ? 'Actualiser l’analyse'
        : 'Analyser';
      dom.similarityAnalyze.disabled = running;
      if (running) {
        dom.similarityAnalyze.setAttribute('aria-busy', 'true');
      } else {
        dom.similarityAnalyze.removeAttribute('aria-busy');
      }
    }
    if (dom.similarityCancel) {
      dom.similarityCancel.hidden = !running;
      dom.similarityCancel.disabled = !running;
      dom.similarityCancel.removeAttribute('aria-busy');
    }

    if (!dom.similarityStatus) {
      return;
    }

    let message = '';
    let kind = 'info';
    if (running) {
      message = total === null
        ? `Analyse en cours · ${processed} traité${processed === 1 ? '' : 's'}`
        : `Analyse en cours · ${processed} / ${total}`;
      if (cached > 0) {
        message += ` · ${cached} en cache`;
      }
      if (errors.length) {
        message += ` · ${errors.length} erreur${errors.length === 1 ? '' : 's'}${similarityErrorSummary(errors)}`;
      }
    } else if (errors.length) {
      kind = 'error';
      message = `Analyse terminée avec ${errors.length} erreur${errors.length === 1 ? '' : 's'}${similarityErrorSummary(errors)}`;
    } else if (cancelled) {
      message = `Analyse interrompue · ${analyzed} analysé${analyzed === 1 ? '' : 's'}`;
    } else if (analyzed > 0) {
      kind = 'success';
      message = `Analyse prête · ${analyzed} analysé${analyzed === 1 ? '' : 's'}`;
    } else if (payload.updated_at) {
      message = 'Analyse terminée sans résultat.';
    } else {
      message = 'Analyse en attente.';
    }
    setMessage(dom.similarityStatus, message, kind);
  }

  function stopSimilarityPolling() {
    if (state.similarityStatusTimer !== null) {
      window.clearTimeout(state.similarityStatusTimer);
      state.similarityStatusTimer = null;
    }
  }

  function scheduleSimilarityPoll() {
    stopSimilarityPolling();
    if (!state.authenticated || state.similarityRunning !== true) {
      return;
    }
    state.similarityStatusTimer = window.setTimeout(() => {
      state.similarityStatusTimer = null;
      void refreshSimilarityStatus({ poll: true });
    }, SIMILARITY_POLL_INTERVAL);
  }

  async function refreshSimilarityStatus({ poll = false } = {}) {
    if (!state.authenticated || (!dom.similarityStatus && !dom.similarityAnalyze && !dom.similarityCancel)) {
      return;
    }
    if (state.similarityStatusRequest) {
      state.similarityStatusRequest.abort();
    }
    const controller = new AbortController();
    state.similarityStatusRequest = controller;
    const sequence = ++state.similarityStatusSequence;
    const wasRunning = state.similarityRunning === true;
    try {
      const payload = await fetchJson('/api/similarity/status', { signal: controller.signal });
      if (sequence !== state.similarityStatusSequence || controller.signal.aborted) {
        return;
      }
      const running = Boolean(payload?.running);
      const cancelRequested = state.similarityCancelRequested;
      renderSimilarityStatus(payload || {});
      if (running) {
        scheduleSimilarityPoll();
      } else {
        stopSimilarityPolling();
        state.similarityCancelRequested = false;
        if (
          wasRunning
          && !cancelRequested
          && payload?.cancelled !== true
          && normaliseSimilarity(dom.similaritySelect?.value)
        ) {
          await refreshPhotosPreservingScroll();
        }
      }
    } catch (error) {
      if (controller.signal.aborted || sequence !== state.similarityStatusSequence) {
        return;
      }
      setMessage(
        dom.similarityStatus,
        displayErrorMessage(error, 'Le statut de similarité est indisponible.'),
        'error',
      );
      if (state.similarityRunning === true) {
        scheduleSimilarityPoll();
      } else if (!poll) {
        stopSimilarityPolling();
      }
    } finally {
      if (state.similarityStatusRequest === controller) {
        state.similarityStatusRequest = null;
      }
    }
  }

  async function startSimilarityAnalysis() {
    if (!dom.similarityAnalyze || dom.similarityAnalyze.disabled || state.similarityRunning === true) {
      return;
    }
    state.similarityCancelRequested = false;
    dom.similarityAnalyze.disabled = true;
    dom.similarityAnalyze.setAttribute('aria-busy', 'true');
    setMessage(dom.similarityStatus, 'Analyse en cours…', 'info');
    try {
      await fetchJson('/api/similarity/analyze', { method: 'POST' });
      state.similarityRunning = true;
      renderSimilarityStatus({ ...(state.similarityStatus || {}), running: true, cancelled: false });
      scheduleSimilarityPoll();
      await refreshSimilarityStatus();
    } catch (error) {
      state.similarityRunning = false;
      setMessage(dom.similarityStatus, displayErrorMessage(error, 'L’analyse n’a pas pu démarrer.'), 'error');
      if (dom.similarityCancel) {
        dom.similarityCancel.hidden = true;
        dom.similarityCancel.disabled = true;
        dom.similarityCancel.removeAttribute('aria-busy');
      }
    } finally {
      if (state.similarityRunning !== true) {
        dom.similarityAnalyze.disabled = false;
        dom.similarityAnalyze.removeAttribute('aria-busy');
      }
    }
  }

  async function cancelSimilarityAnalysis() {
    if (!dom.similarityCancel || dom.similarityCancel.disabled || state.similarityRunning !== true) {
      return;
    }
    state.similarityCancelRequested = true;
    dom.similarityCancel.disabled = true;
    dom.similarityCancel.setAttribute('aria-busy', 'true');
    try {
      await fetchJson('/api/similarity/cancel', { method: 'POST' });
      await refreshSimilarityStatus();
    } catch (error) {
      state.similarityCancelRequested = false;
      dom.similarityCancel.disabled = false;
      setMessage(dom.similarityStatus, displayErrorMessage(error, 'L’arrêt de l’analyse a échoué.'), 'error');
    } finally {
      dom.similarityCancel.removeAttribute('aria-busy');
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
          const statusErrorCount = Array.isArray(payload?.errors)
            ? payload.errors.length
            : finiteNumber(payload?.errors) || 0;
          if (state.uploadPending !== null && state.uploadPending > 0 && statusErrorCount === 0) {
            selectImportedOnly();
          }
          await refreshLibrary();
          scheduleDeletionSummaryRefresh();
          if (state.uploadPending !== null) {
            const errorCount = Math.max(statusErrorCount, state.uploadErrorCount);
            if (errorCount > 0) {
              setMessage(
                dom.uploadSummary,
                `${state.uploadPending} fichier${state.uploadPending === 1 ? '' : 's'} indexé${state.uploadPending === 1 ? '' : 's'} avec ${errorCount} erreur${errorCount === 1 ? '' : 's'}.`,
                'error',
              );
            } else {
              setMessage(
                dom.uploadSummary,
                `${state.uploadPending} fichier${state.uploadPending === 1 ? '' : 's'} indexé${state.uploadPending === 1 ? '' : 's'}.`,
                'success',
              );
            }
            state.uploadPending = null;
            state.uploadErrorCount = 0;
          }
          if (state.scanPending) {
            if (statusErrorCount === 0) {
              setMessage(dom.scanStatus, 'Rescan terminé.', 'success');
            }
            state.scanPending = false;
          }
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

  async function refreshDeletionSummary() {
    if (!state.authenticated) {
      return false;
    }
    if (state.deletionSummaryTimer !== null) {
      window.clearTimeout(state.deletionSummaryTimer);
      state.deletionSummaryTimer = null;
    }
    if (state.deletionSummaryRequest) {
      state.deletionSummaryRequest.abort();
    }
    const controller = new AbortController();
    state.deletionSummaryRequest = controller;
    const sequence = ++state.deletionSummarySequence;
    const results = await Promise.allSettled([
      fetchJson(`/api/photos?${buildZeroCountQuery()}`, { signal: controller.signal }),
      fetchJson('/api/deletions/status', { signal: controller.signal }),
    ]);
    if (sequence !== state.deletionSummarySequence || controller.signal.aborted) {
      if (state.deletionSummaryRequest === controller) {
        state.deletionSummaryRequest = null;
      }
      return false;
    }

    const [zeroResult, statusResult] = results;
    if (zeroResult.status === 'fulfilled') {
      const count = zeroCountFromPayload(zeroResult.value);
      if (count !== null) {
        state.zeroDeletionCount = count;
      }
    }
    if (statusResult.status === 'fulfilled') {
      const scheduled = deletionStatusCountFromPayload(statusResult.value);
      if (scheduled !== null) {
        state.scheduledDeletionCount = scheduled;
      }
      const trashedRaw = statusResult.value?.trashed;
      const trashed = trashedRaw === null || trashedRaw === undefined ? null : Number(trashedRaw);
      if (Number.isInteger(trashed) && trashed >= 0) {
        state.trashedDeletionCount = trashed;
      }
      if (isPresent(statusResult.value?.due_at)) {
        state.deletionDueAt = statusResult.value.due_at;
      }
    }
    renderDeletionSummary();
    if (zeroResult.status === 'rejected' && statusResult.status === 'rejected') {
      setMessage(dom.deletionSummary, 'Le statut de la corbeille est indisponible.', 'error');
    }
    const countAvailable = zeroResult.status === 'fulfilled'
      && zeroCountFromPayload(zeroResult.value) !== null;
    if (state.deletionSummaryRequest === controller) {
      state.deletionSummaryRequest = null;
    }
    return countAvailable;
  }

  function scheduleDeletionSummaryRefresh(delay = 240) {
    if (!state.authenticated) {
      return;
    }
    if (state.deletionSummaryTimer !== null) {
      window.clearTimeout(state.deletionSummaryTimer);
    }
    state.deletionSummaryTimer = window.setTimeout(() => {
      state.deletionSummaryTimer = null;
      void refreshDeletionSummary();
    }, delay);
  }

  function renderDeletionSummary() {
    if (!dom.zeroDeletionCount || !dom.scheduleZeroButton) {
      return;
    }
    const count = state.zeroDeletionCount;
    dom.zeroDeletionCount.textContent = count === null ? '—' : String(count);
    const enabled = Number.isInteger(count) && count > 0;
    dom.scheduleZeroButton.disabled = state.deletionScheduleInFlight || !enabled;
    dom.scheduleZeroButton.setAttribute(
      'aria-label',
      enabled
        ? `Programmer les ${count} médias à 0 étoile pour la corbeille`
        : 'Programmer les médias à 0 étoile pour la corbeille',
    );

    const parts = [];
    if (state.scheduledDeletionCount !== null) {
      parts.push(`${state.scheduledDeletionCount} programmé${state.scheduledDeletionCount === 1 ? '' : 's'}`);
    }
    if (state.trashedDeletionCount !== null && state.trashedDeletionCount > 0) {
      parts.push(`${state.trashedDeletionCount} en corbeille`);
    }
    if (state.scheduledDeletionCount > 0 && state.deletionDueAt) {
      const due = formatDeletionDueDate(state.deletionDueAt);
      if (due) {
        parts.push(`prochaine corbeille le ${due}`);
      }
    }
    setMessage(dom.deletionSummary, parts.join(' · '), 'info');
  }

  function isScheduleZeroDialogOpen() {
    return Boolean(dom.scheduleZeroDialog?.open || dom.scheduleZeroDialog?.hasAttribute('open'));
  }

  function closeScheduleZeroDialog() {
    if (!dom.scheduleZeroDialog) {
      return;
    }
    if (dom.scheduleZeroDialog.open && typeof dom.scheduleZeroDialog.close === 'function') {
      dom.scheduleZeroDialog.close();
    } else {
      dom.scheduleZeroDialog.removeAttribute('open');
    }
    document.body.classList.toggle('modal-open', isLightboxOpen());
    const previousFocus = state.deletionPreviousFocus;
    state.deletionPreviousFocus = null;
    state.deletionDialogCount = null;
    if (previousFocus && typeof previousFocus.focus === 'function' && !previousFocus.hidden) {
      previousFocus.focus({ preventScroll: true });
    }
  }

  function openScheduleZeroDialog(count) {
    state.deletionDialogCount = count;
    dom.scheduleZeroMessage.textContent = confirmationMessageForZeroSchedule(count);
    state.deletionPreviousFocus = document.activeElement;
    if (typeof dom.scheduleZeroDialog.showModal === 'function') {
      dom.scheduleZeroDialog.showModal();
    } else {
      dom.scheduleZeroDialog.setAttribute('open', '');
    }
    document.body.classList.add('modal-open');
    window.setTimeout(() => dom.scheduleZeroConfirm?.focus({ preventScroll: true }), 0);
  }

  async function prepareScheduleZeroConfirmation() {
    if (!dom.scheduleZeroButton || dom.scheduleZeroButton.disabled || state.deletionScheduleInFlight) {
      return;
    }
    dom.scheduleZeroButton.disabled = true;
    dom.scheduleZeroButton.setAttribute('aria-busy', 'true');
    try {
      const refreshed = await refreshDeletionSummary();
      const count = state.zeroDeletionCount;
      if (refreshed && Number.isInteger(count) && count > 0) {
        openScheduleZeroDialog(count);
      }
    } catch (error) {
      setMessage(dom.deletionSummary, displayErrorMessage(error, 'Le nombre de médias n’a pas pu être actualisé.'), 'error');
    } finally {
      dom.scheduleZeroButton.removeAttribute('aria-busy');
      if (!isScheduleZeroDialogOpen() && !state.deletionScheduleInFlight) {
        renderDeletionSummary();
      }
    }
  }

  async function confirmScheduleZero() {
    const count = state.deletionDialogCount;
    if (!Number.isInteger(count) || count <= 0 || state.deletionScheduleInFlight) {
      return;
    }
    state.deletionScheduleInFlight = true;
    dom.scheduleZeroConfirm.disabled = true;
    dom.scheduleZeroCancel.disabled = true;
    dom.scheduleZeroConfirm.setAttribute('aria-busy', 'true');
    try {
      const payload = await fetchJson('/api/deletions/schedule-zero', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({}),
      });
      if (isPresent(payload?.due_at)) {
        state.deletionDueAt = payload.due_at;
      }
      closeScheduleZeroDialog();
      await Promise.allSettled([
        refreshPhotosPreservingScroll(),
        refreshDeletionSummary(),
      ]);
    } catch (error) {
      setMessage(dom.deletionSummary, displayErrorMessage(error, 'La programmation n’a pas pu être enregistrée.'), 'error');
    } finally {
      state.deletionScheduleInFlight = false;
      dom.scheduleZeroConfirm.disabled = false;
      dom.scheduleZeroCancel.disabled = false;
      dom.scheduleZeroConfirm.removeAttribute('aria-busy');
      renderDeletionSummary();
    }
  }

  async function refreshLibrary() {
    await Promise.allSettled([
      loadPhotos({ reset: true }),
      loadFacets(),
    ]);
  }

  async function refreshPhotosPreservingScroll() {
    await loadPhotos({ reset: true, preserveViewport: true });
  }

  async function triggerScan() {
    if (dom.scanButton.disabled) {
      return;
    }
    dom.scanButton.disabled = true;
    dom.scanButton.setAttribute('aria-busy', 'true');
    if (dom.scanButtonLabel) {
      dom.scanButtonLabel.textContent = 'Rescan…';
    }
    state.scanPending = true;
    state.refreshAfterScan = true;
    setMessage(dom.scanStatus, 'Rescan en cours…', 'info');
    try {
      await fetchJson('/api/scan', { method: 'POST' });
      await refreshStatus();
    } catch (error) {
      state.refreshAfterScan = false;
      state.scanPending = false;
      setMessage(dom.scanStatus, displayErrorMessage(error, 'Le scan n’a pas pu démarrer.'), 'error');
    } finally {
      dom.scanButton.disabled = false;
      dom.scanButton.removeAttribute('aria-busy');
      if (dom.scanButtonLabel) {
        dom.scanButtonLabel.textContent = 'Rescanner';
      }
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

  function updateUploadSelection(files) {
    state.selectedFiles = Array.from(files || []);
    const label = formatUploadSelection(state.selectedFiles.length);
    dom.uploadSelection.textContent = label;
    setHidden(dom.uploadSelection, !label);
    dom.uploadSubmit.disabled = !state.selectedFiles.length || state.uploadInFlight || state.uploadPending !== null;
    dom.uploadSubmit.textContent = state.selectedFiles.length
      ? `Importer (${state.selectedFiles.length})`
      : 'Importer';
  }

  function handleUploadSelection(fileList) {
    if (state.uploadInFlight || state.uploadPending !== null) {
      setMessage(dom.uploadSummary, 'Indexation en cours. Vous pourrez importer d’autres fichiers ensuite.', 'info');
      return;
    }
    const files = Array.from(fileList || []);
    clearUploadMessages();
    updateUploadSelection(files);
    if (files.length) {
      setMessage(dom.uploadSummary, 'Sélection prête. Cliquez sur Importer pour envoyer les fichiers.', 'info');
    }
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

  async function uploadFiles(fileList = state.selectedFiles) {
    const files = Array.from(fileList || []);
    if (!files.length || state.uploadInFlight || state.uploadPending !== null) {
      return;
    }
    clearUploadMessages();
    state.uploadInFlight = true;
    dom.uploadSubmit.disabled = true;
    dom.uploadSubmit.setAttribute('aria-busy', 'true');
    dom.uploadSubmit.textContent = 'Import en cours…';
    setMessage(
      dom.uploadSummary,
      `${files.length} fichier${files.length === 1 ? '' : 's'} en cours d’envoi…`,
      'info',
    );
    const formData = new FormData();
    files.forEach((file) => formData.append('files', file, file.name));
    let responseCompleted = false;
    try {
      const payload = await fetchJson('/api/upload', {
        method: 'POST',
        body: formData,
      });
      const accepted = Array.isArray(payload?.accepted)
        ? payload.accepted.length
        : finiteNumber(payload?.accepted);
      const acceptedCount = accepted === null ? files.length : accepted;
      state.uploadPending = acceptedCount;
      state.uploadErrorCount = Array.isArray(payload?.errors)
        ? payload.errors.length
        : finiteNumber(payload?.errors) || 0;
      state.refreshAfterScan = true;
      renderUploadErrors(payload?.errors);
      setMessage(
        dom.uploadSummary,
        `${acceptedCount} fichier${acceptedCount === 1 ? '' : 's'} envoyé${acceptedCount === 1 ? '' : 's'}. Indexation en cours…`,
        'info',
      );
      await refreshStatus();
      responseCompleted = true;
    } catch (error) {
      state.refreshAfterScan = false;
      state.uploadPending = null;
      state.uploadErrorCount = 0;
      setMessage(dom.uploadSummary, displayErrorMessage(error, 'L’import n’a pas pu être envoyé.'), 'error');
    } finally {
      state.uploadInFlight = false;
      dom.uploadSubmit.removeAttribute('aria-busy');
      if (responseCompleted) {
        dom.uploadInput.value = '';
        updateUploadSelection([]);
      } else {
        updateUploadSelection(state.selectedFiles);
      }
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

  function resetLightboxImage() {
    dom.lightboxImage.hidden = true;
    dom.lightboxImage.removeAttribute('src');
    dom.lightboxImage.removeAttribute('alt');
    dom.lightboxImage.removeAttribute('width');
    dom.lightboxImage.removeAttribute('height');
  }

  function resetLightboxVideo() {
    if (!dom.lightboxVideo) {
      return;
    }
    dom.lightboxVideo.pause();
    dom.lightboxVideo.removeAttribute('src');
    dom.lightboxVideo.removeAttribute('poster');
    dom.lightboxVideo.removeAttribute('width');
    dom.lightboxVideo.removeAttribute('height');
    dom.lightboxVideo.hidden = true;
    dom.lightboxVideo.load();
  }

  function resetLightboxMedia() {
    resetViewerZoom();
    resetLightboxImage();
    resetLightboxVideo();
    setHidden(dom.lightboxImageLoader, true);
    dom.lightboxImageFrame.classList.remove('is-missing');
    dom.lightboxImageFrame.classList.remove('is-video');
    dom.lightboxMedia?.classList.remove('is-photo', 'is-video');
  }

  function isPhotoLightbox() {
    const photo = state.items[state.lightboxIndex];
    return Boolean(photo && mediaTypeOf(photo) === 'photo' && dom.lightboxImage && !dom.lightboxImage.hidden);
  }

  function lightboxViewerViewportRect() {
    const rect = dom.lightboxImageFrame?.getBoundingClientRect();
    if (!rect || rect.width <= 0 || rect.height <= 0) {
      return null;
    }
    return {
      left: rect.left,
      top: rect.top,
      right: rect.right,
      bottom: rect.bottom,
      width: rect.width,
      height: rect.height,
    };
  }

  function measureLightboxImageBaseRect() {
    if (!isPhotoLightbox()) {
      return null;
    }
    const image = dom.lightboxImage;
    const previousTransform = image.style.transform;
    image.style.transform = 'none';
    const rect = image.getBoundingClientRect();
    image.style.transform = previousTransform;
    if (rect.width <= 0 || rect.height <= 0) {
      return null;
    }
    return {
      left: rect.left,
      top: rect.top,
      right: rect.right,
      bottom: rect.bottom,
      width: rect.width,
      height: rect.height,
    };
  }

  function captureLightboxImageBaseRect() {
    state.viewerBaseRect = measureLightboxImageBaseRect();
    return state.viewerBaseRect;
  }

  function viewerPointerValues() {
    return [...state.viewerPointers.values()];
  }

  function viewerDistance(first, second) {
    return Math.hypot(second.x - first.x, second.y - first.y);
  }

  function viewerMidpoint(first, second) {
    return {
      x: (first.x + second.x) / 2,
      y: (first.y + second.y) / 2,
    };
  }

  function isViewerInteractiveTarget(target) {
    return Boolean(target?.closest?.(
      'button, a, input, select, textarea, video, .rating-control, .lightbox-loader',
    ));
  }

  function applyLightboxViewerTransform() {
    if (!dom.lightboxImage) {
      return;
    }
    const active = isPhotoLightbox() && state.viewerZoom > 1;
    if (!active) {
      dom.lightboxImage.style.transform = 'none';
    } else {
      dom.lightboxImage.style.transform = `translate3d(${state.viewerPanX}px, ${state.viewerPanY}px, 0) scale(${state.viewerZoom})`;
    }
    dom.lightboxImage.dataset.viewerZoom = String(state.viewerZoom);
    dom.lightboxImage.dataset.viewerPanX = String(state.viewerPanX);
    dom.lightboxImage.dataset.viewerPanY = String(state.viewerPanY);
    setHidden(dom.lightboxResetZoom, !active);
  }

  function resetViewerGestureState() {
    state.viewerPointers.clear();
    state.viewerGesture = null;
    state.viewerGestureHadMultiple = false;
    state.viewerLastTap = null;
    state.swipeStartX = null;
    state.swipeStartY = null;
    state.swipePointerId = null;
  }

  function resetViewerZoom() {
    state.viewerZoom = 1;
    state.viewerPanX = 0;
    state.viewerPanY = 0;
    state.viewerBaseRect = null;
    resetViewerGestureState();
    applyLightboxViewerTransform();
  }

  function ensureViewerBaseRect() {
    return state.viewerBaseRect || captureLightboxImageBaseRect();
  }

  function setLightboxViewerTransform(result) {
    state.viewerZoom = result.zoom;
    state.viewerPanX = Math.abs(result.pan.x) < 0.0001 ? 0 : result.pan.x;
    state.viewerPanY = Math.abs(result.pan.y) < 0.0001 ? 0 : result.pan.y;
    applyLightboxViewerTransform();
  }

  function zoomLightboxAroundPoint(point, nextZoom) {
    if (!isPhotoLightbox()) {
      return;
    }
    const baseRect = ensureViewerBaseRect();
    const viewportRect = lightboxViewerViewportRect();
    if (!baseRect || !viewportRect) {
      return;
    }
    setLightboxViewerTransform(zoomViewerAroundPoint({
      baseRect,
      viewportRect,
      currentZoom: state.viewerZoom,
      currentPan: { x: state.viewerPanX, y: state.viewerPanY },
      nextZoom,
      point,
    }));
  }

  function toggleLightboxDoubleTap(point) {
    const now = performance.now();
    const previous = state.viewerLastTap;
    if (previous && now - previous.time <= 320 && Math.hypot(point.x - previous.x, point.y - previous.y) <= 36) {
      state.viewerLastTap = null;
      if (state.viewerZoom > 1) {
        resetViewerZoom();
      } else {
        zoomLightboxAroundPoint(point, 2.5);
      }
      return;
    }
    state.viewerLastTap = { time: now, x: point.x, y: point.y };
  }

  function beginLightboxPinch() {
    const [first, second] = viewerPointerValues();
    if (!first || !second) {
      return;
    }
    if (!ensureViewerBaseRect()) {
      return;
    }
    state.viewerGestureHadMultiple = true;
    state.viewerGesture = {
      kind: 'pinch',
      startDistance: viewerDistance(first, second),
      startMidpoint: viewerMidpoint(first, second),
      startZoom: state.viewerZoom,
      startPan: { x: state.viewerPanX, y: state.viewerPanY },
    };
    state.swipeStartX = null;
    state.swipeStartY = null;
    state.swipePointerId = null;
  }

  function updateLightboxPinch() {
    const [first, second] = viewerPointerValues();
    const gesture = state.viewerGesture;
    const baseRect = state.viewerBaseRect;
    const viewportRect = lightboxViewerViewportRect();
    if (!first || !second || !gesture || gesture.kind !== 'pinch' || !baseRect || !viewportRect) {
      return;
    }
    setLightboxViewerTransform(pinchViewerTransform({
      baseRect,
      viewportRect,
      startZoom: gesture.startZoom,
      startPan: gesture.startPan,
      startDistance: gesture.startDistance,
      currentDistance: viewerDistance(first, second),
      startMidpoint: gesture.startMidpoint,
      currentMidpoint: viewerMidpoint(first, second),
    }));
  }

  function updateLightboxPan(gesture, event) {
    const baseRect = ensureViewerBaseRect();
    const viewportRect = lightboxViewerViewportRect();
    if (!baseRect || !viewportRect) {
      return;
    }
    const dx = event.clientX - gesture.startX;
    const dy = event.clientY - gesture.startY;
    const pan = clampViewerPan(
      { x: gesture.startPan.x + dx, y: gesture.startPan.y + dy },
      { baseRect, viewportRect, zoom: state.viewerZoom },
    );
    state.viewerPanX = pan.x;
    state.viewerPanY = pan.y;
    gesture.moved = gesture.moved || Math.hypot(dx, dy) > 8;
    if (gesture.moved) {
      state.viewerLastTap = null;
    }
    applyLightboxViewerTransform();
  }

  function onLightboxPointerDown(event) {
    if (!isPhotoLightbox() || isViewerInteractiveTarget(event.target)) {
      return;
    }
    if (event.pointerType === 'mouse' && event.button !== 0) {
      return;
    }
    const pointer = { x: event.clientX, y: event.clientY };
    if (event.pointerType === 'touch') {
      state.viewerPointers.set(event.pointerId, pointer);
      if (state.viewerPointers.size >= 2) {
        beginLightboxPinch();
      } else if (!state.viewerGestureHadMultiple) {
        state.viewerGesture = {
          kind: state.viewerZoom > 1 ? 'pan' : 'swipe',
          pointerId: event.pointerId,
          startX: event.clientX,
          startY: event.clientY,
          startPan: { x: state.viewerPanX, y: state.viewerPanY },
          moved: false,
        };
        state.swipeStartX = state.viewerZoom === 1 ? event.clientX : null;
        state.swipeStartY = state.viewerZoom === 1 ? event.clientY : null;
        state.swipePointerId = state.viewerZoom === 1 ? event.pointerId : null;
      }
      event.currentTarget?.setPointerCapture?.(event.pointerId);
      event.preventDefault();
      return;
    }
    if (event.pointerType === 'mouse' && state.viewerZoom > 1) {
      state.viewerGesture = {
        kind: 'mouse-pan',
        pointerId: event.pointerId,
        startX: event.clientX,
        startY: event.clientY,
        startPan: { x: state.viewerPanX, y: state.viewerPanY },
        moved: false,
      };
      event.currentTarget?.setPointerCapture?.(event.pointerId);
      event.preventDefault();
    }
  }

  function onLightboxPointerMove(event) {
    if (!isPhotoLightbox()) {
      return;
    }
    if (event.pointerType === 'touch' && state.viewerPointers.has(event.pointerId)) {
      state.viewerPointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
      if (state.viewerPointers.size >= 2) {
        if (!state.viewerGesture || state.viewerGesture.kind !== 'pinch') {
          beginLightboxPinch();
        }
        updateLightboxPinch();
        event.preventDefault();
        return;
      }
    }
    const gesture = state.viewerGesture;
    if (!gesture || gesture.pointerId !== event.pointerId || gesture.kind === 'pinch') {
      return;
    }
    if (gesture.kind === 'pan' || gesture.kind === 'mouse-pan') {
      updateLightboxPan(gesture, event);
      event.preventDefault();
      return;
    }
    if (gesture.kind === 'swipe') {
      gesture.moved = gesture.moved || Math.hypot(
        event.clientX - gesture.startX,
        event.clientY - gesture.startY,
      ) > 8;
      if (gesture.moved) {
        state.viewerLastTap = null;
      }
      event.preventDefault();
    }
  }

  function onLightboxPointerUp(event) {
    if (!isPhotoLightbox()) {
      return;
    }
    const gesture = state.viewerGesture;
    if (event.pointerType === 'touch') {
      state.viewerPointers.delete(event.pointerId);
      if (state.viewerPointers.size > 0) {
        event.preventDefault();
        return;
      }
      state.viewerGesture = null;
      state.swipeStartX = null;
      state.swipeStartY = null;
      state.swipePointerId = null;
      if (state.viewerGestureHadMultiple) {
        state.viewerGestureHadMultiple = false;
        state.viewerLastTap = null;
        event.preventDefault();
        return;
      }
      if (!gesture || gesture.pointerId !== event.pointerId) {
        return;
      }
      if ((gesture.kind === 'swipe' || gesture.kind === 'pan') && !gesture.moved) {
        toggleLightboxDoubleTap({ x: event.clientX, y: event.clientY });
        event.preventDefault();
        return;
      }
      if (gesture.kind === 'swipe' && state.viewerZoom === 1) {
        const distanceX = event.clientX - gesture.startX;
        const distanceY = event.clientY - gesture.startY;
        if (Math.abs(distanceX) >= 48 && Math.abs(distanceX) > Math.abs(distanceY) * 1.15) {
          state.viewerLastTap = null;
          navigateLightbox(distanceX < 0 ? 1 : -1);
        }
      }
      event.preventDefault();
      return;
    }
    if (gesture?.pointerId === event.pointerId) {
      state.viewerGesture = null;
    }
  }

  function onLightboxPointerCancel(event) {
    state.viewerPointers.delete(event.pointerId);
    state.viewerGesture = null;
    state.swipeStartX = null;
    state.swipeStartY = null;
    state.swipePointerId = null;
    if (state.viewerPointers.size === 0) {
      state.viewerGestureHadMultiple = false;
      state.viewerLastTap = null;
    }
  }

  function onLightboxWheel(event) {
    if (!isPhotoLightbox() || isViewerInteractiveTarget(event.target)) {
      return;
    }
    event.preventDefault();
    const nextZoom = state.viewerZoom * Math.exp(-event.deltaY * 0.0025);
    zoomLightboxAroundPoint({ x: event.clientX, y: event.clientY }, nextZoom);
  }

  function shareMediaKey(photo = {}) {
    if (photo.id !== undefined && photo.id !== null) {
      return String(photo.id);
    }
    return normaliseAssetUrl(photo.download_url || photo.preview_url) || String(photo.name || 'media');
  }

  function hasNativeFileShare() {
    return Boolean(
      typeof window !== 'undefined'
      && window.isSecureContext
      && typeof navigator !== 'undefined'
      && typeof navigator.share === 'function'
      && typeof navigator.canShare === 'function',
    );
  }

  function hasShareActivation() {
    return Boolean(
      typeof navigator !== 'undefined'
      && navigator.userActivation
      && navigator.userActivation.isActive === true,
    );
  }

  function canShareFile(file) {
    if (!hasNativeFileShare() || !file) {
      return false;
    }
    try {
      return navigator.canShare({ files: [file] }) === true;
    } catch {
      return false;
    }
  }

  function isShareAbort(error) {
    return error?.name === 'AbortError';
  }

  function isCurrentShare(photo, sequence) {
    return sequence === state.shareSequence
      && state.sharePhotoId === shareMediaKey(photo)
      && (dom.lightbox.open || dom.lightbox.hasAttribute('open'));
  }

  function setShareButtonState(button, busy, label) {
    if (!button) {
      return;
    }
    button.disabled = Boolean(busy);
    button.setAttribute('aria-label', label);
    button.setAttribute('title', label);
    if (busy) {
      button.setAttribute('aria-busy', 'true');
    } else {
      button.removeAttribute('aria-busy');
    }
  }

  function setShareStatus(message, kind = '') {
    setMessage(dom.lightboxShareStatus, message, kind);
  }

  function jpegShareFileName(photo) {
    const originalName = shareFileName(photo, '', photo?.name || '');
    const baseName = originalName.replace(/\.[^.]+$/, '') || 'media';
    return `${baseName}.jpg`;
  }

  function resetLightboxShare() {
    state.shareSequence += 1;
    [state.shareController, state.sharePreviewController]
      .filter(Boolean)
      .forEach((controller) => controller.abort());
    state.shareController = null;
    state.sharePreviewController = null;
    state.sharePromise = null;
    state.sharePreviewPromise = null;
    state.shareFiles.clear();
    state.sharePreviewFiles.clear();
    state.sharePhotoId = null;
    state.shareInFlight = false;
    setShareButtonState(dom.lightboxShare, false, 'Partager');
    setShareButtonState(dom.lightboxSharePreview, false, 'Partager le JPEG');
    setHidden(dom.lightboxSharePreview, true);
    setShareStatus('');
  }

  function configureLightboxShare(photo) {
    resetLightboxShare();
    state.sharePhotoId = shareMediaKey(photo);
  }

  function showNativeShareUnavailable() {
    setShareButtonState(dom.lightboxShare, false, 'Partager');
    setShareStatus('Le partage de fichiers nécessite un navigateur compatible en HTTPS.', 'error');
  }

  function showShareReady(preview = false) {
    const button = preview ? dom.lightboxSharePreview : dom.lightboxShare;
    setShareButtonState(button, false, preview ? 'Partager le JPEG' : 'Partager');
    setShareStatus(preview ? 'JPEG prêt. Touchez Partager.' : 'Fichier prêt. Touchez Partager.', 'info');
  }

  function showOriginalShareUnsupported(photo, file) {
    setShareButtonState(dom.lightboxShare, false, 'Partager');
    setShareStatus('Le partage de l’original n’est pas pris en charge sur cet appareil.', 'error');
    const canOfferJpeg = isRawMedia(photo, file?.name)
      && Boolean(normaliseAssetUrl(photo.preview_url));
    setHidden(dom.lightboxSharePreview, !canOfferJpeg);
    if (canOfferJpeg) {
      setShareButtonState(dom.lightboxSharePreview, false, 'Partager le JPEG');
    }
  }

  function showPreviewShareUnsupported() {
    setShareButtonState(dom.lightboxSharePreview, false, 'Partager le JPEG');
    setShareStatus('Le partage du JPEG n’est pas pris en charge sur cet appareil.', 'error');
  }

  function responseHeader(response, name) {
    return response?.headers?.get?.(name) || '';
  }

  function prepareShareFile(photo, { preview = false, sequence = state.shareSequence } = {}) {
    const key = shareMediaKey(photo);
    const cache = preview ? state.sharePreviewFiles : state.shareFiles;
    const cached = cache.get(key);
    if (cached) {
      return Promise.resolve(cached);
    }

    const sourceUrl = normaliseAssetUrl(preview ? photo.preview_url : photo.download_url);
    if (!sourceUrl) {
      return Promise.reject(new Error('Share source unavailable'));
    }

    const controller = new AbortController();
    if (preview) {
      state.sharePreviewController?.abort();
      state.sharePreviewController = controller;
    } else {
      state.shareController?.abort();
      state.shareController = controller;
    }

    const request = fetch(sourceUrl, {
      credentials: 'same-origin',
      signal: controller.signal,
    }).then(async (response) => {
      if (!response.ok) {
        throw new ApiError(`Fichier indisponible (${response.status}).`, response.status);
      }
      const blob = await response.blob();
      if (controller.signal.aborted || !isCurrentShare(photo, sequence)) {
        return null;
      }
      const contentType = responseHeader(response, 'Content-Type');
      const fileName = preview
        ? jpegShareFileName(photo)
        : shareFileName(photo, responseHeader(response, 'Content-Disposition'), sourceUrl);
      const file = new File([blob], fileName, { type: shareFileType(blob.type, contentType) });
      if (controller.signal.aborted || !isCurrentShare(photo, sequence)) {
        return null;
      }
      cache.set(key, file);
      return file;
    }).finally(() => {
      if (preview && state.sharePreviewController === controller) {
        state.sharePreviewController = null;
      }
      if (!preview && state.shareController === controller) {
        state.shareController = null;
      }
    });
    return request;
  }

  function finishNativeShare(error, photo, sequence, preview, button) {
    if (!isCurrentShare(photo, sequence)) {
      return;
    }
    state.shareInFlight = false;
    setShareButtonState(button, false, preview ? 'Partager le JPEG' : 'Partager');
    if (!error || isShareAbort(error)) {
      if (!error) {
        setShareStatus('');
      }
      return;
    }
    if (error.name === 'NotAllowedError') {
      showShareReady(preview);
      return;
    }
    setShareStatus(preview ? 'Le partage du JPEG a échoué. Réessayez.' : 'Le partage a échoué. Réessayez.', 'error');
  }

  function invokeNativeShare(file, photo, { preview = false, sequence = state.shareSequence } = {}) {
    const button = preview ? dom.lightboxSharePreview : dom.lightboxShare;
    if (!isCurrentShare(photo, sequence) || state.shareInFlight) {
      return;
    }
    if (!canShareFile(file)) {
      if (preview) {
        showPreviewShareUnsupported();
      } else {
        showOriginalShareUnsupported(photo, file);
      }
      return;
    }
    if (!hasShareActivation()) {
      showShareReady(preview);
      return;
    }

    let sharePromise;
    try {
      setShareStatus('');
      sharePromise = navigator.share({ files: [file] });
    } catch (error) {
      finishNativeShare(error, photo, sequence, preview, button);
      return;
    }
    state.shareInFlight = true;
    setShareButtonState(button, true, preview ? 'Partage en cours…' : 'Partage en cours…');
    Promise.resolve(sharePromise)
      .then(() => finishNativeShare(null, photo, sequence, preview, button))
      .catch((error) => finishNativeShare(error, photo, sequence, preview, button));
  }

  function handleSharePreparationError(error, photo, sequence, preview = false) {
    if (!isCurrentShare(photo, sequence) || isShareAbort(error)) {
      return;
    }
    const button = preview ? dom.lightboxSharePreview : dom.lightboxShare;
    setShareButtonState(button, false, preview ? 'Partager le JPEG' : 'Partager');
    setShareStatus(
      preview ? 'Le JPEG ne peut pas être préparé. Réessayez.' : 'Le fichier original ne peut pas être préparé. Réessayez.',
      'error',
    );
  }

  function onLightboxShare() {
    const photo = state.items[state.lightboxIndex];
    if (!photo || state.shareInFlight) {
      return;
    }
    if (!hasNativeFileShare()) {
      showNativeShareUnavailable();
      return;
    }
    const key = shareMediaKey(photo);
    const cached = state.shareFiles.get(key);
    if (cached) {
      invokeNativeShare(cached, photo, { sequence: state.shareSequence });
      return;
    }

    const sequence = state.shareSequence;
    setShareButtonState(dom.lightboxShare, true, 'Préparation du fichier…');
    setShareStatus('Préparation du fichier…', 'info');
    const promise = prepareShareFile(photo, { sequence });
    state.sharePromise = promise;
    promise.then((file) => {
      if (!file || !isCurrentShare(photo, sequence)) {
        return;
      }
      setShareButtonState(dom.lightboxShare, false, 'Partager');
      if (!canShareFile(file)) {
        showOriginalShareUnsupported(photo, file);
        return;
      }
      if (hasShareActivation()) {
        invokeNativeShare(file, photo, { sequence });
      } else {
        showShareReady();
      }
    }).catch((error) => handleSharePreparationError(error, photo, sequence))
      .finally(() => {
        if (state.sharePromise === promise) {
          state.sharePromise = null;
        }
      });
  }

  function onLightboxSharePreview() {
    const photo = state.items[state.lightboxIndex];
    if (!photo || state.shareInFlight) {
      return;
    }
    if (!hasNativeFileShare()) {
      showNativeShareUnavailable();
      return;
    }
    const key = shareMediaKey(photo);
    const cached = state.sharePreviewFiles.get(key);
    if (cached) {
      invokeNativeShare(cached, photo, { preview: true, sequence: state.shareSequence });
      return;
    }

    const sequence = state.shareSequence;
    setShareButtonState(dom.lightboxSharePreview, true, 'Préparation du JPEG…');
    setShareStatus('Préparation du JPEG…', 'info');
    const promise = prepareShareFile(photo, { preview: true, sequence });
    state.sharePreviewPromise = promise;
    promise.then((file) => {
      if (!file || !isCurrentShare(photo, sequence)) {
        return;
      }
      setShareButtonState(dom.lightboxSharePreview, false, 'Partager le JPEG');
      if (!canShareFile(file)) {
        showPreviewShareUnsupported();
        return;
      }
      if (hasShareActivation()) {
        invokeNativeShare(file, photo, { preview: true, sequence });
      } else {
        showShareReady(true);
      }
    }).catch((error) => handleSharePreparationError(error, photo, sequence, true))
      .finally(() => {
        if (state.sharePreviewPromise === promise) {
          state.sharePreviewPromise = null;
        }
      });
  }

  function populateLightbox(photo, detail = null) {
    const title = formatExifValue(photo.name);
    const isVideo = mediaTypeOf(photo) === 'video';
    const width = finiteNumber(photo.width);
    const height = finiteNumber(photo.height);
    configureLightboxShare(photo);
    if (dom.lightboxRating) {
      dom.lightboxRating.replaceChildren(makeRatingControl(photo, 'viewer'));
    }
    renderLightboxDeletionStatus(photo);
    dom.lightboxTitle.textContent = title;
    dom.lightboxPosition.textContent = `${state.lightboxIndex + 1} / ${state.items.length}`;
    resetLightboxMedia();
    dom.lightboxImageFrame.classList.toggle('is-video', isVideo);
    dom.lightboxMedia.classList.toggle('is-photo', !isVideo);
    dom.lightboxMedia.classList.toggle('is-video', isVideo);

    if (isVideo) {
      const streamUrl = normaliseAssetUrl(photo.stream_url);
      const posterUrl = normaliseAssetUrl(photo.preview_url || photo.thumb_url);
      if (streamUrl) {
        dom.lightboxVideo.hidden = false;
        dom.lightboxVideo.controls = true;
        dom.lightboxVideo.playsInline = true;
        dom.lightboxVideo.preload = 'metadata';
        dom.lightboxVideo.autoplay = false;
        dom.lightboxVideo.setAttribute('aria-label', title);
        if (posterUrl) {
          dom.lightboxVideo.poster = posterUrl;
        }
        if (width && height) {
          dom.lightboxVideo.width = width;
          dom.lightboxVideo.height = height;
        }
        setHidden(dom.lightboxImageLoader, false);
        dom.lightboxVideo.src = streamUrl;
        dom.lightboxVideo.load();
      }
      dom.lightboxImageFrame.classList.toggle('is-missing', !streamUrl);
    } else {
      const assetUrl = normaliseAssetUrl(photo.preview_url || photo.thumb_url);
      if (assetUrl) {
        dom.lightboxImage.hidden = false;
        dom.lightboxImage.src = assetUrl;
        dom.lightboxImage.alt = title;
        if (width && height) {
          dom.lightboxImage.width = width;
          dom.lightboxImage.height = height;
        }
        setHidden(dom.lightboxImageLoader, false);
      }
      dom.lightboxImageFrame.classList.toggle('is-missing', !assetUrl);
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
      renderLightboxDeletionStatus(findPhotoById(photo.id) || photo);
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
    resetLightboxShare();
    const shouldRefreshFilteredResults = state.ratingFilterRefreshPending;
    state.ratingFilterRefreshPending = false;
    if (!dom.lightbox.open && !dom.lightbox.hasAttribute('open')) {
      resetLightboxMedia();
      if (shouldRefreshFilteredResults && hasActiveMutationFilter()) {
        void refreshPhotosPreservingScroll();
      }
      return;
    }
    if (state.lightboxRequest) {
      state.lightboxRequest.abort();
      state.lightboxRequest = null;
    }
    state.lightboxSequence += 1;
    resetLightboxMedia();
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
    if (shouldRefreshFilteredResults && hasActiveMutationFilter()) {
      void refreshPhotosPreservingScroll();
    }
  }

  function onLightboxKeydown(event) {
    if (event.key === 'Escape') {
      event.preventDefault();
      closeLightbox();
      return;
    }
    const videoHasFocus = dom.lightboxVideo
      && (event.target === dom.lightboxVideo || dom.lightboxVideo.contains(event.target));
    if (videoHasFocus && (event.key === 'ArrowLeft' || event.key === 'ArrowRight')) {
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

  function onLightboxMediaLoaded(event) {
    if (event.currentTarget.hidden) {
      return;
    }
    setHidden(dom.lightboxImageLoader, true);
    dom.lightboxImageFrame.classList.remove('is-missing');
    if (event.currentTarget === dom.lightboxImage && isPhotoLightbox()) {
      captureLightboxImageBaseRect();
      applyLightboxViewerTransform();
    }
  }

  function onLightboxMediaError(event) {
    if (event.currentTarget.hidden) {
      return;
    }
    setHidden(dom.lightboxImageLoader, true);
    event.currentTarget.hidden = true;
    dom.lightboxImageFrame.classList.add('is-missing');
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

  function onFilterChanged(event) {
    clearGroupContext();
    if (event?.target === dom.importedOnlyFilter && dom.importedOnlyFilter.checked) {
      clearFiltersForImportedOnly();
    }
    updateFilterCount();
    void loadPhotos({ reset: true });
  }

  function onViewportResize() {
    if (isLightboxOpen()) {
      resetViewerZoom();
      if (isPhotoLightbox()) {
        captureLightboxImageBaseRect();
      }
    }
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

    if (dom.searchInput) {
      dom.searchInput.addEventListener('input', scheduleSearch);
    }
    dom.sortSelect.addEventListener('change', onFilterChanged);
    dom.mediaTypeSelect.addEventListener('change', onFilterChanged);
    dom.importedOnlyFilter.addEventListener('change', onFilterChanged);
    dom.filtersForm.addEventListener('change', onFilterChanged);
    dom.similaritySelect?.addEventListener('change', onFilterChanged);
    dom.groupBack?.addEventListener('click', backToStacks);
    dom.similarityAnalyze?.addEventListener('click', () => {
      void startSimilarityAnalysis();
    });
    dom.similarityCancel?.addEventListener('click', () => {
      void cancelSimilarityAnalysis();
    });
    dom.gridSize.addEventListener('input', updateGridSize);
    dom.loadMore.addEventListener('click', () => loadPhotos({ reset: false }));
    dom.scheduleZeroButton.addEventListener('click', () => {
      void prepareScheduleZeroConfirmation();
    });
    dom.scheduleZeroCancel.addEventListener('click', closeScheduleZeroDialog);
    dom.scheduleZeroConfirm.addEventListener('click', () => {
      void confirmScheduleZero();
    });
    dom.scheduleZeroDialog.addEventListener('cancel', (event) => {
      event.preventDefault();
      closeScheduleZeroDialog();
    });
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
    dom.uploadInput.addEventListener('change', () => handleUploadSelection(dom.uploadInput.files));
    dom.uploadForm.addEventListener('submit', (event) => {
      event.preventDefault();
      void uploadFiles();
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
    dom.uploadDropzone.addEventListener('drop', (event) => handleUploadSelection(event.dataTransfer.files));

    dom.lightbox.addEventListener('keydown', onLightboxKeydown);
    dom.lightboxClose.addEventListener('click', closeLightbox);
    dom.lightboxPrevious.addEventListener('click', () => navigateLightbox(-1));
    dom.lightboxNext.addEventListener('click', () => navigateLightbox(1));
    dom.lightboxShare.addEventListener('click', onLightboxShare);
    dom.lightboxSharePreview.addEventListener('click', onLightboxSharePreview);
    dom.lightboxImage.addEventListener('load', onLightboxMediaLoaded);
    dom.lightboxImage.addEventListener('error', onLightboxMediaError);
    dom.lightboxVideo.addEventListener('loadedmetadata', onLightboxMediaLoaded);
    dom.lightboxVideo.addEventListener('canplay', onLightboxMediaLoaded);
    dom.lightboxVideo.addEventListener('error', onLightboxMediaError);
    dom.lightboxMedia.addEventListener('pointerdown', onLightboxPointerDown);
    dom.lightboxMedia.addEventListener('pointermove', onLightboxPointerMove);
    dom.lightboxMedia.addEventListener('pointerup', onLightboxPointerUp);
    dom.lightboxMedia.addEventListener('pointercancel', onLightboxPointerCancel);
    dom.lightboxMedia.addEventListener('wheel', onLightboxWheel, { passive: false });
    dom.lightboxResetZoom.addEventListener('click', () => resetViewerZoom());
    dom.lightbox.addEventListener('cancel', (event) => {
      event.preventDefault();
      closeLightbox();
    });
    dom.lightbox.addEventListener('close', () => {
      resetLightboxShare();
      resetLightboxMedia();
      document.body.classList.remove('modal-open');
    });
  }

  async function bootstrap() {
    bindEvents();
    applyUrlFilters();
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
