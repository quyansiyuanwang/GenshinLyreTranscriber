//! Own the complete worker tree, including grandchildren launched by a frozen worker.
use std::io;
use std::process::{Child, Command};

pub struct ProcessTree {
    pub child: Child,
    #[cfg(windows)]
    job: windows_job::Job,
}

impl ProcessTree {
    pub fn spawn(command: &mut Command) -> io::Result<Self> {
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            use windows_sys::Win32::System::Threading::{CREATE_NO_WINDOW, CREATE_SUSPENDED};
            let job = windows_job::Job::new()?;
            // No child code runs before assignment, so grandchildren cannot escape the job.
            command.creation_flags(CREATE_NO_WINDOW | CREATE_SUSPENDED);
            let mut child = command.spawn()?;
            if let Err(error) = job.assign_and_resume(&child) {
                let _ = child.kill();
                let _ = child.wait();
                return Err(error);
            }
            Ok(Self { child, job })
        }
        #[cfg(not(windows))]
        {
            Ok(Self {
                child: command.spawn()?,
            })
        }
    }

    pub fn terminate(&mut self) -> io::Result<()> {
        #[cfg(windows)]
        {
            self.job.terminate()
        }
        #[cfg(not(windows))]
        {
            self.child.kill()
        }
    }

    pub fn finish(&mut self) -> io::Result<()> {
        self.terminate()?;
        self.child.wait()?;
        #[cfg(windows)]
        self.job.wait_empty()?;
        Ok(())
    }
}

impl Drop for ProcessTree {
    fn drop(&mut self) {
        let _ = self.terminate();
        let _ = self.child.wait();
    }
}

#[cfg(windows)]
mod windows_job {
    use std::io;
    use std::mem::{size_of, zeroed};
    use std::os::windows::io::{AsRawHandle, FromRawHandle, OwnedHandle};
    use std::process::Child;
    use std::time::{Duration, Instant};
    use windows_sys::Win32::Foundation::{HANDLE, INVALID_HANDLE_VALUE};
    use windows_sys::Win32::System::Diagnostics::ToolHelp::{
        CreateToolhelp32Snapshot, TH32CS_SNAPTHREAD, THREADENTRY32, Thread32First, Thread32Next,
    };
    use windows_sys::Win32::System::JobObjects::*;
    use windows_sys::Win32::System::Threading::{OpenThread, ResumeThread, THREAD_SUSPEND_RESUME};

    pub struct Job(OwnedHandle);
    impl Job {
        pub fn new() -> io::Result<Self> {
            // SAFETY: null security/name create an unnamed job; the returned handle is owned once.
            unsafe {
                let handle = CreateJobObjectW(std::ptr::null(), std::ptr::null());
                if handle.is_null() {
                    return Err(io::Error::last_os_error());
                }
                let job = Self(OwnedHandle::from_raw_handle(handle));
                let mut limits: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = zeroed();
                limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
                if SetInformationJobObject(
                    handle,
                    JobObjectExtendedLimitInformation,
                    &limits as *const _ as *const _,
                    size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
                ) == 0
                {
                    return Err(io::Error::last_os_error());
                }
                Ok(job)
            }
        }
        fn handle(&self) -> HANDLE {
            self.0.as_raw_handle()
        }
        pub fn assign_and_resume(&self, child: &Child) -> io::Result<()> {
            // SAFETY: child is alive and suspended; handles are owned for the entire call.
            unsafe {
                if AssignProcessToJobObject(self.handle(), child.as_raw_handle()) == 0 {
                    return Err(io::Error::last_os_error());
                }
                let snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0);
                if snapshot == INVALID_HANDLE_VALUE {
                    return Err(io::Error::last_os_error());
                }
                let snapshot = OwnedHandle::from_raw_handle(snapshot);
                let mut entry: THREADENTRY32 = zeroed();
                entry.dwSize = size_of::<THREADENTRY32>() as u32;
                let mut found = Thread32First(snapshot.as_raw_handle(), &mut entry);
                let mut resumed = false;
                while found != 0 {
                    if entry.th32OwnerProcessID == child.id() {
                        let thread = OpenThread(THREAD_SUSPEND_RESUME, 0, entry.th32ThreadID);
                        if thread.is_null() {
                            return Err(io::Error::last_os_error());
                        }
                        let thread = OwnedHandle::from_raw_handle(thread);
                        let previous_count = ResumeThread(thread.as_raw_handle());
                        if previous_count == u32::MAX {
                            return Err(io::Error::last_os_error());
                        }
                        resumed |= previous_count > 0;
                    }
                    found = Thread32Next(snapshot.as_raw_handle(), &mut entry);
                }
                if resumed {
                    Ok(())
                } else {
                    Err(io::Error::other("suspended worker thread was not found"))
                }
            }
        }
        pub fn terminate(&self) -> io::Result<()> {
            // SAFETY: this job handle belongs only to this worker tree.
            if unsafe { TerminateJobObject(self.handle(), 1) } == 0 {
                return Err(io::Error::last_os_error());
            }
            Ok(())
        }
        pub fn wait_empty(&self) -> io::Result<()> {
            let deadline = Instant::now() + Duration::from_secs(10);
            loop {
                // SAFETY: the output buffer has the documented size and lifetime.
                let mut info: JOBOBJECT_BASIC_ACCOUNTING_INFORMATION = unsafe { zeroed() };
                if unsafe {
                    QueryInformationJobObject(
                        self.handle(),
                        JobObjectBasicAccountingInformation,
                        &mut info as *mut _ as *mut _,
                        size_of::<JOBOBJECT_BASIC_ACCOUNTING_INFORMATION>() as u32,
                        std::ptr::null_mut(),
                    )
                } == 0
                {
                    return Err(io::Error::last_os_error());
                }
                if info.ActiveProcesses == 0 {
                    return Ok(());
                }
                if Instant::now() >= deadline {
                    return Err(io::Error::other("worker tree cleanup timed out"));
                }
                std::thread::sleep(Duration::from_millis(10));
            }
        }
    }
}

#[cfg(all(test, windows))]
mod tests {
    use super::*;
    use std::io::{BufRead, BufReader};
    use std::process::Stdio;

    #[test]
    fn cancellation_terminates_descendants_before_reporting_completion() {
        let mut command = Command::new("powershell.exe");
        command.args(["-NoLogo", "-NoProfile", "-NonInteractive", "-Command",
            "$p=Start-Process -FilePath ping.exe -ArgumentList '-t','127.0.0.1' -WindowStyle Hidden -PassThru; [Console]::Out.WriteLine($p.Id); Start-Sleep -Seconds 120"]);
        command.stdout(Stdio::piped()).stderr(Stdio::null());
        let mut tree = ProcessTree::spawn(&mut command).unwrap();
        let mut line = String::new();
        BufReader::new(tree.child.stdout.take().unwrap())
            .read_line(&mut line)
            .unwrap();
        let descendant: u32 = line.trim().parse().unwrap();
        assert_ne!(descendant, tree.child.id());
        tree.finish().unwrap();
        // finish checks the job's ActiveProcesses count, not just the parent exit code.
        assert!(tree.child.try_wait().unwrap().is_some());
    }

    #[test]
    fn spawn_failure_does_not_leave_a_suspended_process() {
        let mut command = Command::new("glt-intentionally-nonexistent-worker.exe");
        assert!(ProcessTree::spawn(&mut command).is_err());
    }
}
