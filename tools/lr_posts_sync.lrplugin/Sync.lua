--[[
Shared sync core. Scans POSTS_ROOT for <post>/<post>.lrsmcol files (written
by ./post.py pull / tools/lr_smart_collection.py) and mirrors each into a
smart collection of the same name inside the SET_NAME collection set.
Existing collections get their rules refreshed, so re-running is always
safe; deleting a post on the site never deletes a collection here.

Each .lrsmcol is a Lua file of the form "s = { ... }" whose .value table is
exactly the searchDescription shape createSmartCollection expects, so the
file's table literal is evaluated and its rules passed straight in.
]]

local LrApplication = import 'LrApplication'
local LrDate = import 'LrDate'
local LrLogger = import 'LrLogger'
local LrFileUtils = import 'LrFileUtils'
local LrPathUtils = import 'LrPathUtils'
local LrPrefs = import 'LrPrefs'
local LrProgressScope = import 'LrProgressScope'
local LrTasks = import 'LrTasks'

-- Log: ~/Library/Logs/Adobe/Lightroom/LrClassicLogs/PostsSync.log
local logger = LrLogger('PostsSync')
logger:enable('logfile')
local function log(...)
    local parts = {}
    for i = 1, select('#', ...) do parts[#parts + 1] = tostring(select(i, ...)) end
    logger:info(table.concat(parts, ' '))
end

local M = {}
M.log = log

M.POSTS_ROOT = '/Volumes/RYAN/Edits/Posts'
M.SET_NAME = 'Posts'
-- Collections were briefly synced as "pulled <post>"; those get renamed back.
M.LEGACY_PREFIX = 'pulled '
-- How long to wait for Lightroom to hand over catalog write access. Asked
-- for in WAIT_STEP slices so the progress bar can count up and Cancel works.
M.WRITE_TIMEOUT = 60
M.WAIT_STEP = 3

local running = false   -- startup sync and menu click must not overlap

local function copy(t)
    local c = {}
    for k, v in pairs(t) do c[k] = v end
    return c
end

-- Progress bar (top-left activity area) that only appears once something is
-- shown on it, so a launch with nothing to sync stays invisible.
local function lazyProgress()
    local scope
    local w = {}
    local function open()
        if not scope then
            scope = LrProgressScope { title = 'Posts Sync' }
            scope:setCancelable(true)
        end
        return scope
    end
    function w:setCaption(c) open():setCaption(c) end
    function w:setPortionComplete(a, b) open():setPortionComplete(a, b) end
    function w:isCanceled() return scope ~= nil and scope:isCanceled() end
    function w:done() if scope then scope:done() end end
    return w
end

local function loadSmcol(path)
    local f = io.open(path, 'rb')
    if not f then return nil, 'unreadable' end
    local contents = f:read('*a')
    f:close()
    -- LR's plugin sandbox has no setfenv, so rather than running the file
    -- (which assigns a global) evaluate its table literal as an expression.
    local body = contents:match('^%s*s%s*=%s*(.*)$')
    if not body then return nil, 'no smart collection definition inside' end
    local chunk = loadstring('return ' .. body)
    if not chunk then return nil, 'not valid Lua' end
    local ok, s = pcall(chunk)
    if not ok then return nil, tostring(s) end
    if type(s) ~= 'table' or type(s.value) ~= 'table' then
        return nil, 'no smart collection definition inside'
    end
    return s
end

-- Runs `fn` inside one catalog write. Returns nil on success, or a message.
-- Errors inside are caught here so one bad collection never sinks the rest,
-- and the write status is checked so "never got access" is told apart from
-- "failed while writing".
local function write(catalog, progress, label, fn)
    local t0 = LrDate.currentTime()
    local innerErr, status
    repeat
        if progress:isCanceled() then
            log('  ', label, 'canceled while waiting for catalog access')
            return 'Canceled.'
        end
        -- Some catalog errors (e.g. "name is not unique") are raised when
        -- the write commits, outside fn, so guard the whole call too.
        local called, st = LrTasks.pcall(function()
            return catalog:withWriteAccessDo('Sync post collections', function()
                local ok, e = LrTasks.pcall(fn)
                if not ok then innerErr = tostring(e) end
            end, { timeout = M.WAIT_STEP })
        end)
        if called then
            status = st
        else
            status, innerErr = 'executed', tostring(st)
        end
        local waited = LrDate.currentTime() - t0
        if status ~= 'executed' then
            progress:setCaption(string.format(
                'Waiting for Lightroom to allow catalog changes (%ds)...', waited))
        end
    until status == 'executed' or LrDate.currentTime() - t0 >= M.WRITE_TIMEOUT
    local secs = string.format('%.1fs', LrDate.currentTime() - t0)
    if status ~= 'executed' then
        log('  ', label, 'write status=' .. tostring(status), 'after', secs)
        return 'Lightroom did not allow catalog changes for ' .. M.WRITE_TIMEOUT
            .. 's (status: ' .. tostring(status) .. '). Something else is holding'
            .. ' the catalog, often a stuck plugin task. Restart Lightroom and try again.'
    elseif innerErr then
        log('  ', label, 'FAILED after', secs, innerErr)
        return innerErr
    end
    log('  ', label, 'ok in', secs)
    return nil
end

-- Runs one full sync. Must be called from an async task.
-- `source` ('menu' | 'startup') only labels the log.
-- Returns { synced = n, upToDate = n, bad = {names}, err = 'human message' | nil }.
function M.sync(source)
    if running then
        log('sync requested by', source, 'while another sync is running; skipped')
        return { synced = 0, bad = {}, err = 'A sync is already running - wait for it to finish.' }
    end
    running = true
    local progress = lazyProgress()
    local ok, res = LrTasks.pcall(M._sync, source, progress)
    progress:done()
    running = false
    if not ok then
        log('sync crashed:', res)
        error(res, 0)
    end
    if res.err then log('sync finished with error:', res.err) end
    return res
end

function M._sync(source, progress)
    local t0 = LrDate.currentTime()
    log('---- sync start (' .. tostring(source) .. ')')
    if source ~= 'startup' then progress:setCaption('Scanning ' .. M.POSTS_ROOT .. '...') end
    if LrFileUtils.exists(M.POSTS_ROOT) ~= 'directory' then
        return { synced = 0, bad = {},
                 err = M.POSTS_ROOT .. ' is not reachable. Is the RYAN drive mounted?' }
    end

    local jobs, bad = {}, {}
    for dir in LrFileUtils.directoryEntries(M.POSTS_ROOT) do
        if LrFileUtils.exists(dir) == 'directory' then
            local name = LrPathUtils.leafName(dir)
            local smcol = LrPathUtils.child(dir, name .. '.lrsmcol')
            if LrFileUtils.exists(smcol) == 'file' then
                local doc, why = loadSmcol(smcol)
                if doc then
                    local attrs = LrFileUtils.fileAttributes(smcol) or {}
                    jobs[#jobs + 1] = { name = name, doc = doc,
                                        stamp = tostring(attrs.fileModificationDate) }
                    log('  found', name, '(' .. #doc.value .. ' photo rule(s))')
                else
                    bad[#bad + 1] = name .. ' (' .. why .. ')'
                    log('  bad file', name, why)
                end
            end
        end
    end
    log('  scan done in', string.format('%.1fs', LrDate.currentTime() - t0),
        #jobs, 'collection file(s)')
    if #jobs == 0 then
        return { synced = 0, bad = bad,
                 err = #bad == 0 and ('No .lrsmcol files found under ' .. M.POSTS_ROOT
                       .. '. Run  ./post.py pull  first.') or nil }
    end

    local catalog = LrApplication.activeCatalog()
    -- Skip posts whose .lrsmcol is unchanged since its last successful sync
    -- and whose collection still exists, so a normal launch writes nothing.
    local prefs = LrPrefs.prefsForPlugin()
    local stamps = prefs.syncedStamps or {}
    local existing = {}
    for _, s in ipairs(catalog:getChildCollectionSets()) do
        if s:getName() == M.SET_NAME then
            for _, c in ipairs(s:getChildCollections()) do existing[c:getName()] = true end
        end
    end
    local todo, upToDate = {}, 0
    for _, job in ipairs(jobs) do
        if stamps[job.name] == job.stamp and existing[job.name] then
            upToDate = upToDate + 1
        else
            todo[#todo + 1] = job
        end
    end
    log('  ', #todo, 'to sync,', upToDate, 'already up to date')
    if #todo == 0 then
        return { synced = 0, upToDate = upToDate, bad = bad }
    end
    jobs = todo

    local set
    progress:setCaption('Creating the "' .. M.SET_NAME .. '" collection set...')
    local err = write(catalog, progress, 'collection set "' .. M.SET_NAME .. '"', function()
        set = catalog:createCollectionSet(M.SET_NAME, nil, true)
    end)
    if err then return { synced = 0, bad = bad, err = err } end

    local byName = {}
    for _, c in ipairs(set:getChildCollections()) do byName[c:getName()] = c end

    -- One write per collection: short locks, and a failure stays local.
    local synced, lastErr = 0, nil
    for i, job in ipairs(jobs) do
        local title = job.name
        if progress:isCanceled() then
            log('  canceled before', title)
            break
        end
        progress:setPortionComplete(i - 1, #jobs)
        progress:setCaption(string.format('Syncing "%s" (%d of %d)...', title, i, #jobs))
        local col = byName[title]
        local legacy = byName[M.LEGACY_PREFIX .. job.name]
        local e
        -- Collection synced under the old "pulled " name: rename it in its own
        -- write first. (Renaming and then createSmartCollection in the same
        -- write made LR try to create a second one: "name is not unique".)
        if not col and legacy and legacy:isSmartCollection() then
            e = write(catalog, progress, 'rename ' .. legacy:getName() .. ' -> ' .. title,
                      function() legacy:setName(title) end)
            col = legacy
            if not e then
                byName[title] = legacy
            else
                log('  ', 'keeping the old name for now; updating its rules anyway')
            end
        end
        if col then
            e = write(catalog, progress, title, function()
                col:setSearchDescription(job.doc.value)
            end)
        else
            e = write(catalog, progress, title, function()
                col = catalog:createSmartCollection(title, job.doc.value, set, false)
                if not col then error('createSmartCollection returned nil') end
            end)
            if not e then byName[title] = col end
        end
        if e then
            bad[#bad + 1] = job.name .. ' (' .. e .. ')'
            lastErr = e
        else
            synced = synced + 1
            stamps[job.name] = job.stamp
            prefs.syncedStamps = copy(stamps)   -- save as we go; a fresh table so LrPrefs notices
        end
    end
    log('---- sync done:', synced, 'synced,', #bad, 'skipped, total',
        string.format('%.1fs', LrDate.currentTime() - t0))
    progress:setPortionComplete(1, 1)
    if synced == 0 then
        return { synced = 0, bad = {}, err = 'Nothing synced. Last error: ' .. tostring(lastErr) }
    end
    return { synced = synced, upToDate = upToDate, bad = bad }
end

return M
