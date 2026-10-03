App = window.App || {};

(function(exports, $) {
    /*
     * Compare page.
     *
     * Both sides are configured in one form. Choosing a database loads its
     * table list; choosing a table (or switching sides) derives the
     * available columns and renders the key-column checkboxes, pre-checking
     * the common columns. Submit reloads only the results fragment.
     *
     * Conditions can be changed faster than the server answers. Every
     * request is aborted when a newer one starts and responses carry a
     * sequence number, so the last submission's results always win.
     */
    var state = null;

    function Compare() {}

    Compare.prototype.initialize = function(options) {
        this.columnsUrl = options.columnsUrl;
        this.compareUrl = options.compareUrl;
        this.tables = options.tables || {};
        this.selectedKeys = options.selectedKeys || [];
        this.xhr = null;
        this.keysXhrA = this.keysXhrB = null;
        this.seq = 0;
        this.keysSeq = 0;
        this.$form = $('#compare-form');
        this.$results = $('#compare-results');
        if (!this.$form.length) return;

        var self = this;
        $('.compare-dataset').on('change', function() {
            self.loadTables($(this));
            self.scheduleKeys();
        });
        $('input[name$="_source"]').on('change', function() {
            self.updateSourceVisibility($(this).closest('.compare-side'));
            self.scheduleKeys();
        });
        $('.compare-table').on('change', function() { self.scheduleKeys(); });
        $('.compare-sql').on('input', function() { self.scheduleKeys(); });

        $('.compare-side').each(function() {
            self.updateSourceVisibility($(this));
        });
        // Populate table selects for sides that already have a database.
        $('.compare-dataset').each(function() {
            var $sel = $(this);
            if ($sel.val()) self.loadTables($sel, true);
        });
        // Rebuild the key checkboxes from the (already rendered) sides.
        this.refreshKeys();

        this.$form.on('submit', function(e) {
            e.preventDefault();
            self.submit();
        });
        // Paging and filtering are real links; load them as fragments so
        // the form state never has to round-trip.
        this.$results.on('click', 'a.diff-page-link, a.diff-filter-link',
            function(e) {
                e.preventDefault();
                self.loadUrl($(this).attr('href'));
            });
    };

    Compare.prototype.sideOf = function($el) {
        return $el.closest('.compare-side').data('side');
    };

    Compare.prototype.updateSourceVisibility = function($side) {
        var source = $side.find('input[name$="_source"]:checked').val();
        $side.find('.compare-table-row').toggle(source !== 'query');
        $side.find('.compare-query-row').toggle(source === 'query');
    };

    Compare.prototype.loadTables = function($select, keepValue) {
        var side = this.sideOf($select),
            key = $select.val(),
            tables = this.tables[key] || [],
            $tableSelect = $('#' + side + '-table');
        $tableSelect.empty().prop('disabled', !tables.length);
        $tableSelect.append($('<option value="">— select table —</option>'));
        var current = $tableSelect.data('value') || $tableSelect.val();
        for (var i = 0; i < tables.length; i++) {
            var $opt = $('<option></option>').attr('value', tables[i])
                                             .text(tables[i]);
            $tableSelect.append($opt);
        }
        if (keepValue && current) $tableSelect.val(current);
    };

    Compare.prototype.sideParams = function(side) {
        var $side = $('.compare-side[data-side="' + side + '"]'),
            source = $side.find('input[name$="_source"]:checked').val();
        return {
            dataset: $side.find('.compare-dataset').val(),
            source: source,
            table: source === 'query' ? '' : $side.find('.compare-table').val(),
            sql: source === 'query' ? $side.find('.compare-sql').val() : ''
        };
    };

    Compare.prototype.validateSide = function(params) {
        if (!params.dataset) return 'Choose a database.';
        if (params.source === 'table' && !params.table) {
            return 'Choose a table.';
        }
        if (params.source === 'query' && !$.trim(params.sql)) {
            return 'Enter a SELECT query.';
        }
        return '';
    };

    /* Rebuild key columns shortly after typing stops, so rapid edits do
       not fire a request per keystroke. */
    Compare.prototype.scheduleKeys = function() {
        var self = this;
        clearTimeout(this._keysTimer);
        this._keysTimer = setTimeout(function() { self.refreshKeys(); },
                                     300);
    };

    Compare.prototype.fetchColumns = function(side, params) {
        return $.ajax({
            url: this.columnsUrl,
            data: params,
            dataType: 'json'
        }).then(function(data) {
            if (data && data.error) {
                return $.Deferred().reject(data.error).promise();
            }
            return data.columns || [];
        }, function(xhr) {
            var msg = 'Could not read columns.';
            try { msg = JSON.parse(xhr.responseText).error || msg; }
            catch (e) {}
            return $.Deferred().reject(msg).promise();
        });
    };

    Compare.prototype.refreshKeys = function() {
        var self = this,
            paramsA = this.sideParams('a'),
            paramsB = this.sideParams('b'),
            errA = this.validateSide(paramsA),
            errB = this.validateSide(paramsB),
            $keys = $('#compare-keys');
        // Preserve the user's ticks when columns are rebuilt.
        var checked = $('.compare-key:checked').map(function() {
            return $(this).val();
        }).get();
        if (!checked.length) checked = this.selectedKeys;
        $('.compare-side-error').hide().text('');
        if (errA || errB) {
            if (errA) $('.compare-side[data-side="a"] .compare-side-error')
                .text(errA).show();
            if (errB) $('.compare-side[data-side="b"] .compare-side-error')
                .text(errB).show();
            $keys.html('<small class="text-muted">Complete both sides ' +
                       'to choose key columns.</small>');
            return;
        }
        // Abort in-flight column reads and tag this round; a late answer
        // for an older side configuration must not repaint the checkboxes.
        var mySeq = ++this.keysSeq;
        if (this.keysXhrA) this.keysXhrA.abort();
        if (this.keysXhrB) this.keysXhrB.abort();
        $keys.html('<small class="text-muted">Reading columns…</small>');
        this.keysXhrA = this.fetchColumns('a', paramsA);
        this.keysXhrB = this.fetchColumns('b', paramsB);
        $.when(this.keysXhrA, this.keysXhrB).done(
            function(colsA, colsB) {
                if (mySeq !== self.keysSeq) return;
                var common = colsA.filter(function(c) {
                    return colsB.indexOf(c) !== -1;
                });
                if (!common.length) {
                    $keys.html('<div class="alert alert-warning py-2 ' +
                        'mb-0">The two sides have no columns in common.' +
                        '</div>');
                    return;
                }
                var $list = $('<div></div>');
                common.forEach(function(col) {
                    var id = 'key-' + col.replace(/[^a-zA-Z0-9_-]/g, '_'),
                        isChecked = checked.indexOf(col) !== -1;
                    $list.append(
                        '<div class="form-check form-check-inline">' +
                        '<input class="form-check-input compare-key" ' +
                        'type="checkbox" name="key" id="' + id + '" ' +
                        'value="' + $('<i>').text(col).html() + '"' +
                        (isChecked ? ' checked' : '') + ' /> ' +
                        '<label class="form-check-label" for="' + id + '">' +
                        $('<i>').text(col).html() + '</label></div>');
                });
                $keys.empty().append($list);
                self._keysLoaded = true;
            }).fail(function(xhr, status) {
                if (status === 'abort' || mySeq !== self.keysSeq) return;
                var msg = (xhr && xhr.responseJSON &&
                           xhr.responseJSON.error) || 'Could not read columns.';
                $keys.html('<div class="alert alert-danger py-2 mb-0">' +
                    $('<i>').text(msg).html() + '</div>');
            });
    };

    Compare.prototype.serialize = function() {
        var p = this.sideParams('a'),
            data = {
                a_dataset: p.dataset, a_source: p.source,
                a_table: p.table, a_sql: p.sql
            };
        p = this.sideParams('b');
        data.b_dataset = p.dataset;
        data.b_source = p.source;
        data.b_table = p.table;
        data.b_sql = p.sql;
        var keys = $('.compare-key:checked').map(function() {
            return $(this).val();
        }).get();
        data.key = keys;
        return data;
    };

    Compare.prototype.submit = function() {
        var data = this.serialize();
        if (!data.key.length) {
            this.refreshKeys();
            this.$results.html('<div class="alert alert-warning">Choose ' +
                'one or more key columns.</div>');
            return;
        }
        this.loadUrl(this.compareUrl + '?' + $.param(data, true));
    };

    Compare.prototype.loadUrl = function(url) {
        var self = this,
            mySeq = ++this.seq;
        if (this.xhr) this.xhr.abort();  // Only the newest request matters.
        this.$results.html('<small class="text-muted">Comparing…</small>');
        this.xhr = $.ajax({
            url: url,
            data: {fragment: 1},
            traditional: true
        }).done(function(html) {
            if (mySeq !== self.seq) return;  // A newer request superseded it.
            self.$results.html(html);
        }).fail(function(xhr, status) {
            if (status === 'abort') return;
            if (mySeq !== self.seq) return;
            self.$results.html('<div class="alert alert-danger">The ' +
                'comparison failed.</div>');
        });
    };

    exports.Compare = new Compare();
})(App, jQuery);
