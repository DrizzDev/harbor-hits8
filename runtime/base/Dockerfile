# drizz-android-twins:api34 - the phone every task's `android` service is built from.
#
# The same device as the runs the tasks were mined from (tasks/newhost-22oct on the EC2 bench): Android 14,
# system-images;android-34;google_apis;x86_64 (rooted userdebug, Chrome included), pixel_6 at 1080x2400 @420 dpi,
# 4096 MB, 2 cores, the twin apps installed with every permission granted, Gmail and Google Calendar disabled.
# The emulator needs /dev/kvm at run time (compose passes it through); building needs no KVM.
#
#   bash runtime/base/build.sh            # builds drizz-android-twins:api34 from runtime/base/apks/*.apk
FROM ubuntu:24.04

ARG CMDLINE_TOOLS=commandlinetools-linux-11076708_latest.zip
ARG ABI=x86_64
ENV DEBIAN_FRONTEND=noninteractive \
    ANDROID_SDK_ROOT=/opt/android-sdk ANDROID_HOME=/opt/android-sdk ANDROID_AVD_HOME=/root/.android/avd \
    ANDROID_SERIAL=emulator-5554 BENCH_ROOT=/opt/drizz/bench
ENV PATH=/opt/android-sdk/platform-tools:/opt/android-sdk/emulator:/opt/android-sdk/cmdline-tools/latest/bin:$PATH

RUN apt-get update && apt-get install -y --no-install-recommends \
      openjdk-17-jre-headless unzip curl ca-certificates python3 python3-venv tzdata \
      libpulse0 libgl1 libnss3 libxcomposite1 libxcursor1 libxdamage1 libxi6 libxtst6 libasound2t64 \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p $ANDROID_SDK_ROOT/cmdline-tools && cd /tmp \
    && curl -sSfLo clt.zip https://dl.google.com/android/repository/${CMDLINE_TOOLS} \
    && unzip -q clt.zip -d $ANDROID_SDK_ROOT/cmdline-tools && mv $ANDROID_SDK_ROOT/cmdline-tools/cmdline-tools $ANDROID_SDK_ROOT/cmdline-tools/latest \
    && rm clt.zip \
    && yes | sdkmanager --licenses >/dev/null \
    && sdkmanager "platform-tools" "emulator" "system-images;android-34;google_apis;${ABI}" >/dev/null

RUN echo no | avdmanager -s create avd -n phone -k "system-images;android-34;google_apis;${ABI}" -d pixel_6 \
    && sed -i -E '/^(hw\.lcd\.width|hw\.lcd\.height|hw\.lcd\.density|hw\.ramSize|hw\.cpu\.ncore|hw\.keyboard|disk\.dataPartition\.size)=/d' $ANDROID_AVD_HOME/phone.avd/config.ini \
    && printf 'hw.lcd.width=1080\nhw.lcd.height=2400\nhw.lcd.density=420\nhw.ramSize=4096\nhw.cpu.ncore=2\nhw.keyboard=yes\ndisk.dataPartition.size=8G\n' >> $ANDROID_AVD_HOME/phone.avd/config.ini

# The bench's device code needs PyYAML; the phone service needs the MCP SDK (v1 API).
RUN python3 -m venv /opt/drizz/venv && /opt/drizz/venv/bin/pip install --no-cache-dir "pyyaml==6.0.2" "mcp==1.30.0"
ENV PATH=/opt/drizz/venv/bin:$PATH

COPY apks/ /opt/drizz/apks/
COPY APKS.sha256 /opt/drizz/APKS.sha256
RUN cd /opt/drizz/apks && sha256sum -c /opt/drizz/APKS.sha256
COPY boot_phone.sh /opt/drizz/boot_phone.sh
RUN chmod +x /opt/drizz/boot_phone.sh && mkdir -p /state

EXPOSE 8000
CMD ["/opt/drizz/boot_phone.sh"]
