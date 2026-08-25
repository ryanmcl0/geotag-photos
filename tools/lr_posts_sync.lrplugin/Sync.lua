--[[
Shared sync core. Scans POSTS_ROOT for <post>/<post>.lrsmcol files (written
by ./post.py pull / tools/lr_smart_collection.py) and mirrors each into a
smart collection of the same name inside the SET_NAME collection set.
Existing collections get their rules refreshed, so re-running is always
safe; deleting a post on the site never deletes a collection here.

Each .lrsmcol is a Lua file of the form "s = { ... }" whose .value table is
exactly the searchDescription shape createSmartCollection expects, so the
file is loaded in a sandboxed environment and its rules passed straight in.
]]

local LrApplication = import 'LrApplication'
local LrFileUtils = import 'LrFileUtils'
local LrPathUtils = import 'LrPathUtils'

local M = {}

M.POSTS_ROOT = '/Volumes/RYAN/Edits/Posts'
M.SET_NAME = 'Posts'

local function loadSmcol(path)
    local f = io.open(path, 'rb')
    if not f then return nil, 'unreadable' end
    local contents = f:read('*a')
    f:close()
    local chunk = loadstring(contents)
    if not chunk then return nil, 'not valid Lua' end
    local env = {}
    setfenv(chunk, env)
    local ok, err = pcall(chunk)
    if not ok then return nil, tostring(err) end
    if type(env.s) ~= 'table' or type(env.s.value) ~= 'table' then
        return nil, 'no smart collection definition inside'
    end
    return env.s
end

-- Runs one full sync. Must be called from an async task.
-- Returns { synced = n, bad = {names}, err = 'human message' | nil }.
function M.sync()
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
                    jobs[#jobs + 1] = { name = name, doc = doc }
                else
                    bad[#bad + 1] = name .. ' (' .. why .. ')'
                end
            end
        end
    end
    if #jobs == 0 then
        return { synced = 0, bad = bad,
                 err = #bad == 0 and ('No .lrsmcol files found under ' .. M.POSTS_ROOT
                       .. '. Run  ./post.py pull  first.') or nil }
    end

    local catalog = LrApplication.activeCatalog()
    local synced = false
    catalog:withWriteAccessDo('Sync post collections', function()
        local set = catalog:createCollectionSet(M.SET_NAME, nil, true)
        for _, job in ipairs(jobs) do
            -- returnExisting=true hands back the current collection of that
            -- name; setSearchDescription then applies any rule changes.
            local col = catalog:createSmartCollection(job.name, job.doc.value, set, true)
            if col then
                pcall(function() col:setSearchDescription(job.doc.value) end)
            end
        end
        synced = true
    end, { timeout = 15 })

    if not synced then
        return { synced = 0, bad = bad, err = 'The catalog was busy - try again in a moment.' }
    end
    return { synced = #jobs, bad = bad }
end

return M
