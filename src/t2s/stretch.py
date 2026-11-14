from array import array
from typing import Final

frame_milliseconds: Final = 30
overlap_milliseconds: Final = 15
tolerance_milliseconds: Final = 5
coarse_offset_step: Final = 4
correlation_sample_step: Final = 4


def milliseconds_to_samples(rate: int, milliseconds: int) -> int:
    return max(rate * milliseconds // 1000, 1)


def offset_cost(
    samples: array[int],
    template: array[int],
    start_frame: int,
    channels: int,
    frame_length: int,
) -> int | None:
    start = start_frame * channels
    candidate = samples[start : start + len(template)]
    if (
        len(candidate) < len(template)
        or start_frame + frame_length > len(samples) // channels
    ):
        return None
    return sum(
        abs(first - second)
        for first, second in zip(
            candidate[::correlation_sample_step],
            template[::correlation_sample_step],
            strict=True,
        )
    )


def best_offset(
    samples: array[int],
    template: array[int],
    input_position: int,
    tolerance: int,
    channels: int,
    frame_length: int,
) -> int:
    coarse = min(
        (
            (cost, offset)
            for offset in range(-tolerance, tolerance + 1, coarse_offset_step)
            if (
                cost := offset_cost(
                    samples, template, input_position + offset, channels, frame_length
                )
            )
            is not None
        ),
        default=(0, 0),
    )
    refined = min(
        (
            (cost, offset)
            for offset in range(
                coarse[1] - coarse_offset_step + 1, coarse[1] + coarse_offset_step
            )
            if (
                cost := offset_cost(
                    samples, template, input_position + offset, channels, frame_length
                )
            )
            is not None
        ),
        default=coarse,
    )
    return refined[1]


def blended_tail(output: array[int], segment: array[int], overlap: int) -> None:
    blended = min(overlap, len(output), len(segment))
    for index in range(blended):
        position = len(output) - blended + index
        fading = output[position]
        rising = segment[index]
        output[position] = (fading * (blended - index) + rising * index) // blended
    output.extend(segment[blended:])


def time_stretched(data: bytes, rate: int, channels: int, speed: float) -> bytes:
    if speed == 1.0:
        return data
    samples = array("h", data)
    if not samples or channels < 1:
        return data
    frame_length = milliseconds_to_samples(rate, frame_milliseconds)
    overlap_length = min(
        milliseconds_to_samples(rate, overlap_milliseconds), frame_length - 1
    )
    tolerance = milliseconds_to_samples(rate, tolerance_milliseconds)
    input_hop = max(round((frame_length - overlap_length) * speed), 1)
    total_frames = len(samples) // channels
    output: array[int] = array("h")
    input_position = 0
    offset = 0
    while True:
        start = input_position + offset
        if start + frame_length > total_frames:
            break
        segment = samples[start * channels : (start + frame_length) * channels]
        if output:
            blended_tail(output, segment, overlap_length * channels)
        else:
            output.extend(segment)
        input_position += input_hop
        offset = best_offset(
            samples,
            output[-overlap_length * channels :],
            input_position,
            tolerance,
            channels,
            frame_length,
        )
    tail = samples[max(input_position + offset, 0) * channels :]
    if tail:
        blended_tail(output, tail, overlap_length * channels)
    return output.tobytes()
