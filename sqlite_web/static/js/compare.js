App = window.App || {};

(function(exports, $) {
    /*
     * Compare page:
     *  - "Load columns" probes both sides and renders key checkboxes.
     *  - Tabs, pagination and the form refresh the results panel in place.
     *  - A monotonically increasing sequence number guarantees the last
     *    panel shown always corresponds to the last request issued, even
     *    when earlier responses come back later.
     */
    var seq = 0;

    function currentParams(tab, page) {
        var params = $('#compare-form').serializeArray();
        params.push({name: 'tab', value: tab || 'all'});
        if (page) params.push({name: 'page', value: String(page)});
        return params;
    }

    function refresh(tab, page) {
        var mySeq = ++seq,
            panel = $('#compare-results'),
            params = currentParams(tab, page);
        panel.addClass('compare-loading');
        // The active tab must survive the panel replacement.
        var activeTab = tab || $('.compare-tab.active').data('tab') || 'all';
        $.get(window.COMPARE_RESULTS_URL, params)
            .done(function(html) {
                if (mySeq !== seq) return;  // A newer request superseded us.
                panel.html(html);
                panel.find('.compare-tab[data-tab="' + activeTab + '"]').addClass('active');
                bindPanel();
            })
            .fail(function() {
                if (mySeq !== seq) return;
                panel.html('<div class="alert alert-danger">Could not load the comparison.</div>');
            })
            .always(function() {
                if (mySeq === seq) panel.removeClass('compare-loading');
            });
    }

    function keyCheckbox(side, column, checked) {
        var safe = $('<div></div>');
        return $('<div class="form-check mr-3 compare-key-option"></div>')
            .append($('<input class="form-check-input compare-key" type="checkbox" name="key">')
                .val(column).prop('checked', !!checked))
            .append($('<label class="form-check-label"></label>').text(column)
                .prepend(safe));
    }

    function loadColumns() {
        var hint = $('.compare-keys-hint'),
            container = $('#compare-keys'),
            button = $('#compare-load-columns');
        container.find('.compare-key-option').remove();
        var sides = ['l', 'r'],
            results = {},
            failed = null;

        function merge() {
            if (failed) {
                hint.text(failed).removeClass('d-none');
                button.prop('disabled', false);
                return;
            }
            if (!('l' in results) || !('r' in results)) return;
            var leftCols = results.l, rightCols = results.r;
            var shared = leftCols.filter(function(c) { return rightCols.indexOf(c) !== -1; });
            if (!shared.length) {
                hint.text('The two queries have no column names in common.').removeClass('d-none');
                button.prop('disabled', false);
                return;
            }
            var existing = container.find('.compare-key-existing input').map(function() {
                return $(this).val();
            }).get();
            shared.forEach(function(col) {
                container.append(keyCheckbox(null, col, existing.indexOf(col) !== -1));
            });
            hint.addClass('d-none');
            button.prop('disabled', false);
        }

        button.prop('disabled', true);
        hint.text('Loading columns...').removeClass('d-none');
        sides.forEach(function(side) {
            var dsSelect = $('.compare-dataset[data-side="' + side + '"]'),
                sql = $('.compare-sql[data-side="' + side + '"]').val();
            $.get(window.COMPARE_COLUMNS_URL, {side: side, dataset: dsSelect.val(), sql: sql})
                .done(function(data) { results[side] = data.columns || []; merge(); })
                .fail(function(xhr) {
                    var msg = 'Side ' + (side === 'l' ? 'left' : 'right') + ': ';
                    try { msg += (JSON.parse(xhr.responseText) || {}).error || 'error'; }
                    catch (e) { msg += 'could not load columns'; }
                    failed = msg; merge();
                });
        });
    }

    function bindPanel() {
        $('.compare-tab').on('click', function(e) {
            e.preventDefault();
            refresh($(this).data('tab'), 1);
        });
        $('.compare-page').on('click', function(e) {
            if ($(this).parent().hasClass('disabled')) {
                e.preventDefault();
                return;
            }
            e.preventDefault();
            refresh($('.compare-tab.active').data('tab') || 'all',
                    $(this).data('page'));
        });
    }

    exports.initializeCompare = function() {
        if (!$('#compare-form').length) return;
        $('#compare-load-columns').on('click', loadColumns);
        $('#compare-form').on('submit', function(e) {
            e.preventDefault();
            refresh('all', 1);
        });
        bindPanel();
    };
})(App, jQuery);
